import sqlite3

DB_PATH = "/data/db/tracker.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    employee_id TEXT UNIQUE NOT NULL,
    consent_signed_at TEXT NOT NULL,
    enrolled_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS worker_face_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    source_photo_ref TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_channel_id INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL,
    site_label TEXT,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    last_polled_at TEXT,
    skip_to_latest INTEGER NOT NULL DEFAULT 0,
    latest_known_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    caption TEXT,
    photo_path TEXT,
    video_path TEXT,
    processed_at TEXT,
    UNIQUE (channel_id, telegram_message_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_messages_unprocessed ON raw_messages (processed_at) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    confidence REAL NOT NULL,
    photo_path TEXT,
    video_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_sightings_worker_ts ON sightings (worker_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS field_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER REFERENCES workers(id) ON DELETE CASCADE,
    sighting_id INTEGER REFERENCES sightings(id) ON DELETE SET NULL,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    parsed_fields TEXT
);
CREATE INDEX IF NOT EXISTS idx_field_reports_worker_ts ON field_reports (worker_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS unrecognized_faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    embedding BLOB NOT NULL,
    crop_path TEXT NOT NULL,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    sightings_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_unrecognized_faces_last_seen ON unrecognized_faces (last_seen);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs (created_at DESC);

CREATE TABLE IF NOT EXISTS channel_watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT UNIQUE NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    address TEXT,
    map_url TEXT,
    telegram_contact TEXT,
    wechat_contact TEXT,
    captured_info TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA_SQL)
    # channels.last_polled_at was added after channels already shipped, so
    # CREATE TABLE IF NOT EXISTS won't retrofit it onto an existing
    # database -- this covers upgrades; it's a no-op on fresh installs
    # since the CREATE TABLE above already includes the column.
    try:
        conn.execute("ALTER TABLE channels ADD COLUMN last_polled_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE channels ADD COLUMN skip_to_latest INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE raw_messages ADD COLUMN video_path TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE sightings ADD COLUMN video_path TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE channels ADD COLUMN latest_known_message_id INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE channels ADD COLUMN identifier TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE channels ADD COLUMN reset_scan INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE channels ADD COLUMN branch_id INTEGER REFERENCES branches(id)")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE channels ADD COLUMN captured_info TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    _migrate_tg_channels_setting_to_watchlist(conn)
    _migrate_site_labels_to_branches(conn)
    _dedupe_sightings(conn)
    return conn


def get_or_create_branch(conn, name):
    """Returns the branch id for `name`, creating it if it doesn't exist
    yet. Branch names are kept in sync with channels.site_label -- this is
    the one place that relationship is created/maintained."""
    conn.execute("INSERT INTO branches (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
    return conn.execute("SELECT id FROM branches WHERE name = ?", (name,)).fetchone()[0]


def _migrate_site_labels_to_branches(conn):
    """Backfills a Branch entity for every channel that already has a
    site_label but isn't linked to a branch yet -- e.g. channels labelled
    before the Branches feature existed. Safe to run every connection: it's
    a no-op once a channel is linked (branch_id IS NULL is the only
    trigger), and it never touches an already-linked channel."""
    rows = conn.execute(
        "SELECT id, site_label FROM channels WHERE branch_id IS NULL AND site_label IS NOT NULL AND site_label != ''"
    ).fetchall()
    for channel_id, site_label in rows:
        branch_id = get_or_create_branch(conn, site_label)
        conn.execute("UPDATE channels SET branch_id = ? WHERE id = ?", (branch_id, channel_id))
    conn.commit()


def _dedupe_sightings(conn):
    """Removes duplicate sighting rows (same worker matched twice against
    the same message -- can happen if face-worker was ever restarted
    mid-processing, or if two instances briefly ran concurrently during a
    redeploy) and adds a unique index so it can't happen again. Keeps the
    earliest row of each duplicate group."""
    conn.execute(
        "DELETE FROM sightings WHERE id NOT IN (SELECT MIN(id) FROM sightings GROUP BY worker_id, raw_message_id)"
    )
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sightings_worker_message ON sightings(worker_id, raw_message_id)"
        )
    except sqlite3.IntegrityError:
        pass
    conn.commit()


def _migrate_tg_channels_setting_to_watchlist(conn):
    """One-time migration: the old comma-separated TG_CHANNELS setting is
    replaced by the channel_watchlist table (managed from Admin -> Channels
    instead of Admin -> Settings). Seeds the watchlist from whatever was
    already configured, so upgrading doesn't silently stop watching
    channels. Guarded by a persistent flag (not "is the table empty"),
    since an admin intentionally clearing the watchlist later shouldn't
    cause it to be silently re-seeded from stale data."""
    already_migrated = conn.execute(
        "SELECT 1 FROM app_settings WHERE key = 'CHANNEL_WATCHLIST_MIGRATED'"
    ).fetchone()
    if already_migrated:
        return
    old_value = conn.execute("SELECT value FROM app_settings WHERE key = 'TG_CHANNELS'").fetchone()
    if old_value and old_value[0]:
        for ident in old_value[0].split(","):
            ident = ident.strip()
            if ident:
                conn.execute(
                    "INSERT INTO channel_watchlist (identifier) VALUES (?) ON CONFLICT(identifier) DO NOTHING",
                    (ident,),
                )
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('CHANNEL_WATCHLIST_MIGRATED', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()


def get_setting(conn, key, default=None):
    """Plain (unencrypted) settings only -- face-worker never touches the
    Telegram credentials, so it has no need for the Fernet/cryptography
    dependency the other two services carry."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None and row[0] is not None else default


def log_event(conn, service, level, message):
    conn.execute(
        "INSERT INTO logs (service, level, message, created_at) VALUES (?, ?, ?, datetime('now'))",
        (service, level, message),
    )
    conn.commit()


def purge_old_logs(conn, keep_last=1000):
    conn.execute(
        "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (keep_last,)
    )
    conn.commit()
