-- Telegram Field-Worker Location Tracker schema
-- Privacy note: worker_face_embeddings only ever holds embeddings for enrolled,
-- consenting workers. No table exists for unmatched/unknown faces by design.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE workers (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    employee_id     TEXT UNIQUE NOT NULL,
    consent_signed_at TIMESTAMPTZ NOT NULL,
    enrolled_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes           TEXT
);

CREATE TABLE worker_face_embeddings (
    id              SERIAL PRIMARY KEY,
    worker_id       INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    embedding       vector(512) NOT NULL,
    source_photo_ref TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON worker_face_embeddings USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE channels (
    id                  SERIAL PRIMARY KEY,
    telegram_channel_id BIGINT UNIQUE NOT NULL,
    name                TEXT NOT NULL,
    site_label          TEXT,
    last_message_id     BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE raw_messages (
    id                  SERIAL PRIMARY KEY,
    channel_id          INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    telegram_message_id BIGINT NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL,
    caption             TEXT,
    photo_path          TEXT,
    processed_at        TIMESTAMPTZ,
    UNIQUE (channel_id, telegram_message_id)
);
CREATE INDEX ON raw_messages (processed_at) WHERE processed_at IS NULL;

CREATE TABLE sightings (
    id              SERIAL PRIMARY KEY,
    worker_id       INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    raw_message_id  INTEGER NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    confidence      REAL NOT NULL,
    photo_path      TEXT
);
CREATE INDEX ON sightings (worker_id, timestamp DESC);

CREATE TABLE field_reports (
    id              SERIAL PRIMARY KEY,
    worker_id       INTEGER REFERENCES workers(id) ON DELETE CASCADE,
    sighting_id     INTEGER REFERENCES sightings(id) ON DELETE SET NULL,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    raw_message_id  INTEGER NOT NULL REFERENCES raw_messages(id) ON DELETE CASCADE,
    timestamp       TIMESTAMPTZ NOT NULL,
    raw_text        TEXT NOT NULL,
    parsed_fields   JSONB
);
CREATE INDEX ON field_reports (worker_id, timestamp DESC);
