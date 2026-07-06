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
    last_message_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_message_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    caption TEXT,
    photo_path TEXT,
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
    photo_path TEXT
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
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA_SQL)
    return conn


def get_setting(conn, key, default=None):
    """Plain (unencrypted) settings only -- face-worker never touches the
    Telegram credentials, so it has no need for the Fernet/cryptography
    dependency the other two services carry."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None and row[0] is not None else default
