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


def crop_face(image_path, bbox, out_path, padding=0.25):
    """Crops the face region (plus a small padding margin) out of
    image_path and writes it to out_path, for use as a reviewable thumbnail
    in the unrecognized-faces admin queue."""
    img = cv2.imread(image_path)
    if img is None:
        return False
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    pad_x, pad_y = (x2 - x1) * padding, (y2 - y1) * padding
    x1 = max(0, int(x1 - pad_x))
    y1 = max(0, int(y1 - pad_y))
    x2 = min(w, int(x2 + pad_x))
    y2 = min(h, int(y2 + pad_y))
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return False
    cv2.imwrite(out_path, crop)
    return True
