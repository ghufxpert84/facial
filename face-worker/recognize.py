"""Face matching against the enrolled worker gallery.

Privacy-critical behaviour: for any detected face that does not match an
enrolled worker above MATCH_THRESHOLD, nothing is returned and nothing is
persisted — no embedding, no crop, no record of any kind. This module never
writes to worker_face_embeddings; only enroll.py does that, for consenting
workers.
"""
from face_model import get_faces


def match_faces(conn, image_path, threshold):
    """Returns a list of (worker_id, confidence) for faces in image_path that
    match an enrolled worker at or above threshold. Non-matching faces are
    silently dropped (not returned, not stored)."""
    matches = []
    for face in get_faces(image_path):
        embedding = face.normed_embedding.tolist()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT worker_id, 1 - (embedding <=> %s::vector) AS similarity
                FROM worker_face_embeddings
                ORDER BY embedding <=> %s::vector
                LIMIT 1
                """,
                (embedding, embedding),
            )
            row = cur.fetchone()
        if row is None:
            continue
        worker_id, similarity = row
        if similarity >= threshold:
            matches.append((worker_id, float(similarity)))
        # else: face discarded, nothing stored — this is intentional.
    return matches
