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
from face_model import extract_video_frame
from recognize import match_faces
from report_extractor import extract_field_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("face-worker")
SERVICE_NAME = "face-worker"


def fetch_unprocessed(conn):
    return conn.execute(
        "SELECT id, channel_id, timestamp, caption, photo_path, video_path FROM raw_messages "
        "WHERE processed_at IS NULL ORDER BY id"
    ).fetchall()


def process_message(conn, msg_id, channel_id, timestamp, caption, photo_path, video_path, match_threshold):
    sighting_by_worker = {}

    # Videos have no single "the photo" to match against, so pull one
    # representative frame and run the exact same matching pipeline on it
    # that photos already use.
    match_image_path = photo_path
    if not match_image_path and video_path and os.path.exists(video_path):
        frame_path = f"{video_path}.frame.jpg"
        if extract_video_frame(video_path, frame_path):
            match_image_path = frame_path
        else:
            log.warning("Could not extract a frame from %s", video_path)
            log_event(conn, SERVICE_NAME, "warning", f"Could not extract a frame from {video_path}")

    if match_image_path and os.path.exists(match_image_path):
        matches, unmatched = match_faces(conn, match_image_path, match_threshold)
        for worker_id, confidence in matches:
            cur = conn.execute(
                """
                INSERT INTO sightings (worker_id, channel_id, raw_message_id, timestamp, confidence, photo_path, video_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (worker_id, channel_id, msg_id, timestamp, confidence, match_image_path, video_path),
            )
            sighting_by_worker[worker_id] = cur.lastrowid
        candidates.stage_unmatched_faces(conn, unmatched, match_image_path, channel_id, msg_id, match_threshold)

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
    """Deletes expired raw_messages (cascading to sightings/field_reports)
    and every file referenced by them -- including video files and their
    extracted frames, which live on sightings.photo_path/video_path rather
    than raw_messages (a video's raw_messages row has no photo_path)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    paths = set()
    for row in conn.execute("SELECT photo_path, video_path FROM raw_messages WHERE timestamp < ?", (cutoff,)):
        paths.update(p for p in row if p)
    for row in conn.execute("SELECT photo_path, video_path FROM sightings WHERE timestamp < ?", (cutoff,)):
        paths.update(p for p in row if p)
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
