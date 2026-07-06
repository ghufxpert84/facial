"""Stages unmatched faces into the unrecognized_faces review queue, with
de-duplication against already-pending candidates so repeated appearances
of the same unknown person don't spam the queue with duplicate rows.

Nothing here creates a worker or a permanent biometric record -- an admin
must explicitly name (enroll) or dismiss each candidate via the dashboard's
Admin -> Unrecognized Faces page. Unreviewed candidates auto-expire after
a configurable retention window (see purge_expired_candidates), so this is
a temporary staging area, not an "unknown persons" database.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np

from face_model import crop_face

CANDIDATE_DIR = "/data/db/candidate_faces"


def _find_similar_candidate(conn, embedding, threshold):
    best_id, best_similarity = None, -1.0
    for cand_id, cand_embedding in conn.execute("SELECT id, embedding FROM unrecognized_faces"):
        similarity = float(np.dot(embedding, np.frombuffer(cand_embedding, dtype=np.float32)))
        if similarity > best_similarity:
            best_id, best_similarity = cand_id, similarity
    return best_id if best_id is not None and best_similarity >= threshold else None


def stage_unmatched_faces(conn, unmatched_faces, image_path, channel_id, raw_message_id, dedupe_threshold):
    if not unmatched_faces:
        return
    os.makedirs(CANDIDATE_DIR, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    for face in unmatched_faces:
        embedding = np.asarray(face.normed_embedding, dtype=np.float32)
        existing_id = _find_similar_candidate(conn, embedding, dedupe_threshold)
        if existing_id is not None:
            conn.execute(
                "UPDATE unrecognized_faces SET last_seen = ?, sightings_count = sightings_count + 1 WHERE id = ?",
                (now, existing_id),
            )
            continue
        crop_path = os.path.join(CANDIDATE_DIR, f"{uuid.uuid4().hex}.jpg")
        if not crop_face(image_path, face.bbox, crop_path):
            continue
        conn.execute(
            """
            INSERT INTO unrecognized_faces (embedding, crop_path, channel_id, raw_message_id, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (embedding.tobytes(), crop_path, channel_id, raw_message_id, now, now),
        )
    conn.commit()


def purge_expired_candidates(conn, retention_hours):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
    rows = conn.execute("SELECT crop_path FROM unrecognized_faces WHERE last_seen < ?", (cutoff,)).fetchall()
    conn.execute("DELETE FROM unrecognized_faces WHERE last_seen < ?", (cutoff,))
    conn.commit()
    for (crop_path,) in rows:
        try:
            os.remove(crop_path)
        except FileNotFoundError:
            pass
