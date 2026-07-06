"""Main face-worker loop: consumes raw_messages queued by telegram-listener,
runs face recognition + field-report extraction, and purges expired data.

MATCH_THRESHOLD/RETENTION_DAYS/POLL_INTERVAL_SECONDS/
UNRECOGNIZED_RETENTION_HOURS are read from the app_settings table (set via
the dashboard's Admin -> Settings page) fresh on every loop iteration, so
changes there apply without a redeploy.
"""
import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone

import candidates
from db import get_conn, get_setting, log_event, purge_old_logs
from recognize import match_faces
from report_extractor import extract_field_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("face-worker")
SERVICE_NAME = "face-worker"


def fetch_unprocessed(conn):
    return conn.execute(
        "SELECT id, channel_id, timestamp, caption, photo_path FROM raw_messages "
        "WHERE processed_at IS NULL ORDER BY id"
    ).fetchall()


def process_message(conn, msg_id, channel_id, timestamp, caption, photo_path, match_threshold):
    sighting_by_worker = {}

    if photo_path and os.path.exists(photo_path):
        matches, unmatched = match_faces(conn, photo_path, match_threshold)
        for worker_id, confidence in matches:
            cur = conn.execute(
                """
                INSERT INTO sightings (worker_id, channel_id, raw_message_id, timestamp, confidence, photo_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (worker_id, channel_id, msg_id, timestamp, confidence, photo_path),
            )
            sighting_by_worker[worker_id] = cur.lastrowid
        candidates.stage_unmatched_faces(conn, unmatched, photo_path, channel_id, msg_id, match_threshold)

    parsed = extract_field_report(caption)
    if parsed is not None:
        worker_id = next(iter(sighting_by_worker), None)
        sighting_id = sighting_by_worker.get(worker_id)
        conn.execute(
            """
            INSERT INTO field_reports (worker_id, sighting_id, channel_id, raw_message_id, timestamp, raw_text, parsed_fields)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (worker_id, sighting_id, channel_id, msg_id, timestamp, caption, json.dumps(parsed)),
        )

    conn.execute("UPDATE raw_messages SET processed_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), msg_id))
    conn.commit()


def purge_expired(conn, retention_days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    paths = [
        row[0]
        for row in conn.execute(
            "SELECT photo_path FROM raw_messages WHERE timestamp < ? AND photo_path IS NOT NULL", (cutoff,)
        )
    ]
    conn.execute("DELETE FROM raw_messages WHERE timestamp < ?", (cutoff,))
    conn.commit()
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def main():
    conn = get_conn()
    log.info("face-worker started")
    log_event(conn, SERVICE_NAME, "info", "face-worker started")
    while True:
        match_threshold = float(get_setting(conn, "MATCH_THRESHOLD", "0.45"))
        retention_days = int(get_setting(conn, "RETENTION_DAYS", "90"))
        poll_interval = int(get_setting(conn, "POLL_INTERVAL_SECONDS", "60"))
        unrecognized_retention_hours = int(get_setting(conn, "UNRECOGNIZED_RETENTION_HOURS", "72"))
        try:
            for row in fetch_unprocessed(conn):
                process_message(conn, *row, match_threshold)
            purge_expired(conn, retention_days)
            candidates.purge_expired_candidates(conn, unrecognized_retention_hours)
            purge_old_logs(conn)
        except Exception as e:
            log.exception("error in worker loop, will retry next interval")
            conn.rollback()
            log_event(conn, SERVICE_NAME, "error", f"Error in worker loop: {e}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
