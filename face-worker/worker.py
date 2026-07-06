"""Main face-worker loop: consumes raw_messages queued by telegram-listener,
runs face recognition + field-report extraction, and purges expired data."""
import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone

from db import get_conn
from recognize import match_faces
from report_extractor import extract_field_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("face-worker")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.45"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))


def fetch_unprocessed(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, channel_id, timestamp, caption, photo_path FROM raw_messages "
            "WHERE processed_at IS NULL ORDER BY id"
        )
        return cur.fetchall()


def process_message(conn, msg_id, channel_id, timestamp, caption, photo_path):
    sighting_by_worker = {}

    if photo_path and os.path.exists(photo_path):
        for worker_id, confidence in match_faces(conn, photo_path, MATCH_THRESHOLD):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sightings (worker_id, channel_id, raw_message_id, timestamp, confidence, photo_path)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (worker_id, channel_id, msg_id, timestamp, confidence, photo_path),
                )
                sighting_by_worker[worker_id] = cur.fetchone()[0]

    parsed = extract_field_report(caption)
    if parsed is not None:
        worker_id = next(iter(sighting_by_worker), None)
        sighting_id = sighting_by_worker.get(worker_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO field_reports (worker_id, sighting_id, channel_id, raw_message_id, timestamp, raw_text, parsed_fields)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (worker_id, sighting_id, channel_id, msg_id, timestamp, caption, json.dumps(parsed)),
            )

    with conn.cursor() as cur:
        cur.execute("UPDATE raw_messages SET processed_at = now() WHERE id = %s", (msg_id,))
    conn.commit()


def purge_expired(conn):
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    with conn.cursor() as cur:
        cur.execute("SELECT photo_path FROM raw_messages WHERE timestamp < %s AND photo_path IS NOT NULL", (cutoff,))
        paths = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM raw_messages WHERE timestamp < %s", (cutoff,))
    conn.commit()
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def main():
    conn = get_conn()
    log.info("face-worker started, threshold=%s retention_days=%s", MATCH_THRESHOLD, RETENTION_DAYS)
    while True:
        try:
            for row in fetch_unprocessed(conn):
                process_message(conn, *row)
            purge_expired(conn)
        except Exception:
            log.exception("error in worker loop, will retry next interval")
            conn.rollback()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
