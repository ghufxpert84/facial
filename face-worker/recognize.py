"""Face matching against the enrolled worker gallery.

Privacy-critical behaviour: for any detected face that does not match an
enrolled worker above MATCH_THRESHOLD, nothing is returned and nothing is
persisted — no embedding, no crop, no record of any kind. This module never
writes to worker_face_embeddings; only enroll.py does that, for consenting
workers.

Matching is brute-force cosine similarity in Python rather than a database
vector index: the enrolled gallery is expected to be small (tens to low
hundreds of reference embeddings), so this is simpler and fast enough
without needing a vector extension.
"""
import numpy as np

from face_model import get_faces


def match_faces(conn, image_path, threshold):
    """Returns a list of (worker_id, confidence) for faces in image_path that
    match an enrolled worker at or above threshold. Non-matching faces are
    silently dropped (not returned, not stored)."""
    gallery = [
        (worker_id, np.frombuffer(embedding, dtype=np.float32))
        for worker_id, embedding in conn.execute("SELECT worker_id, embedding FROM worker_face_embeddings")
    ]

    matches = []
    for face in get_faces(image_path):
        query = np.asarray(face.normed_embedding, dtype=np.float32)
        best_worker_id, best_similarity = None, -1.0
        for worker_id, embedding in gallery:
            similarity = float(np.dot(query, embedding))
            if similarity > best_similarity:
                best_worker_id, best_similarity = worker_id, similarity
        if best_worker_id is not None and best_similarity >= threshold:
            matches.append((best_worker_id, best_similarity))
        # else: face discarded, nothing stored — this is intentional.
    return matches
