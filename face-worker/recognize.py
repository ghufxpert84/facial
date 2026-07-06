"""Face matching against the enrolled worker gallery.

Privacy-critical behaviour: a face that doesn't match an enrolled worker is
never turned into a worker or auto-enrolled by this module. It's returned
as "unmatched" so the caller (worker.py) can stage it in the
unrecognized_faces review queue, where an admin must explicitly name it
(recording consent at that moment) or dismiss it -- there is no path from
"unmatched face" to a permanent biometric record without that human step.

Matching is brute-force cosine similarity in Python rather than a database
vector index: the enrolled gallery is expected to be small (tens to low
hundreds of reference embeddings), so this is simpler and fast enough
without needing a vector extension.
"""
import numpy as np

from face_model import get_faces


def match_faces(conn, image_path, threshold):
    """Returns (matches, unmatched_faces).

    matches: list of (worker_id, confidence) for faces that matched an
    enrolled worker at or above threshold.

    unmatched_faces: list of face objects (from insightface, with
    .normed_embedding/.bbox) for faces that did not match — candidates for
    the review queue, not yet persisted anywhere.
    """
    gallery = [
        (worker_id, np.frombuffer(embedding, dtype=np.float32))
        for worker_id, embedding in conn.execute("SELECT worker_id, embedding FROM worker_face_embeddings")
    ]

    matches = []
    unmatched = []
    for face in get_faces(image_path):
        query = np.asarray(face.normed_embedding, dtype=np.float32)
        best_worker_id, best_similarity = None, -1.0
        for worker_id, embedding in gallery:
            similarity = float(np.dot(query, embedding))
            if similarity > best_similarity:
                best_worker_id, best_similarity = worker_id, similarity
        if best_worker_id is not None and best_similarity >= threshold:
            matches.append((best_worker_id, best_similarity))
        else:
            unmatched.append(face)
    return matches, unmatched
