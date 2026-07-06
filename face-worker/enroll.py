"""Enroll a consenting worker into the face-recognition gallery.

Usage:
    docker compose run --rm face-worker python enroll.py \\
        --name "Jane Doe" --employee-id E123 \\
        --consent-date 2026-07-01 \\
        --photos /data/enrolled/jane/1.jpg /data/enrolled/jane/2.jpg

Each reference photo must contain exactly one clear face of the worker being
enrolled. --consent-date is required — this tool refuses to create a worker
record without a documented consent date, since worker_face_embeddings holds
biometric data.
"""
import argparse
import sys

import numpy as np

from db import get_conn
from face_model import get_faces


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--employee-id", required=True)
    parser.add_argument("--consent-date", required=True, help="ISO date, e.g. 2026-07-01, when consent was signed")
    parser.add_argument("--notes", default=None)
    parser.add_argument("--photos", required=True, nargs="+", help="One or more reference photo paths")
    args = parser.parse_args()

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO workers (name, employee_id, consent_signed_at, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(employee_id) DO UPDATE SET name = excluded.name, notes = excluded.notes
        """,
        (args.name, args.employee_id, args.consent_date, args.notes),
    )
    worker_id = conn.execute("SELECT id FROM workers WHERE employee_id = ?", (args.employee_id,)).fetchone()[0]

    enrolled = 0
    for photo_path in args.photos:
        faces = get_faces(photo_path)
        if len(faces) != 1:
            print(f"SKIP {photo_path}: expected exactly 1 face, found {len(faces)}", file=sys.stderr)
            continue
        embedding = np.asarray(faces[0].normed_embedding, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT INTO worker_face_embeddings (worker_id, embedding, source_photo_ref) VALUES (?, ?, ?)",
            (worker_id, embedding, photo_path),
        )
        enrolled += 1

    conn.commit()
    print(f"Enrolled worker_id={worker_id} ({args.name}) with {enrolled}/{len(args.photos)} reference photos")


if __name__ == "__main__":
    main()
