"""Thin wrapper around insightface, sized for CPU-only use on modest hardware
(Synology DS220+ / Celeron J4025 — no GPU, 2 cores)."""
import cv2
from insightface.app import FaceAnalysis

_app = None


def get_app():
    global _app
    if _app is None:
        _app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"])
        _app.prepare(ctx_id=-1, det_size=(640, 640))
    return _app


def get_faces(image_path):
    """Returns a list of face objects (each with .normed_embedding, .bbox) for
    every face detected in the image. Empty list if none found or the file
    can't be read."""
    img = cv2.imread(image_path)
    if img is None:
        return []
    return get_app().get(img)
