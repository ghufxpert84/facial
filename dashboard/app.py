import json
import os
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext

from db import get_conn

DASHBOARD_USER = os.environ["DASHBOARD_USER"]
DASHBOARD_PASSWORD_HASH = os.environ.get("DASHBOARD_PASSWORD_HASH", "")

pwd_context = CryptContext(schemes=["bcrypt"])
security = HTTPBasic()

app = FastAPI()
templates = Jinja2Templates(directory="templates")


def verify(credentials: HTTPBasicCredentials = Depends(security)):
    user_ok = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    pass_ok = bool(DASHBOARD_PASSWORD_HASH) and pwd_context.verify(credentials.password, DASHBOARD_PASSWORD_HASH)
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/")
def worker_directory(request: Request, user: str = Depends(verify)):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT w.id, w.name, w.employee_id, s.timestamp, c.name, c.site_label
            FROM workers w
            LEFT JOIN (
                SELECT s1.* FROM sightings s1
                WHERE s1.timestamp = (SELECT MAX(s2.timestamp) FROM sightings s2 WHERE s2.worker_id = s1.worker_id)
            ) s ON s.worker_id = w.id
            LEFT JOIN channels c ON c.id = s.channel_id
            ORDER BY w.name
            """
        ).fetchall()
    finally:
        conn.close()

    workers = [
        {"id": r[0], "name": r[1], "employee_id": r[2], "last_seen": r[3], "channel_name": r[4], "site_label": r[5]}
        for r in rows
    ]
    return templates.TemplateResponse("index.html", {"request": request, "workers": workers})


@app.get("/workers/{worker_id}")
def worker_detail(worker_id: int, request: Request, user: str = Depends(verify)):
    conn = get_conn()
    try:
        w = conn.execute(
            "SELECT id, name, employee_id, consent_signed_at, notes FROM workers WHERE id = ?", (worker_id,)
        ).fetchone()
        if w is None:
            raise HTTPException(status_code=404, detail="Worker not found")

        sightings = conn.execute(
            """
            SELECT s.timestamp, c.name, c.site_label, s.confidence
            FROM sightings s JOIN channels c ON c.id = s.channel_id
            WHERE s.worker_id = ? ORDER BY s.timestamp DESC
            """,
            (worker_id,),
        ).fetchall()

        reports = conn.execute(
            "SELECT timestamp, raw_text, parsed_fields FROM field_reports WHERE worker_id = ? ORDER BY timestamp DESC",
            (worker_id,),
        ).fetchall()
    finally:
        conn.close()

    worker = {"id": w[0], "name": w[1], "employee_id": w[2], "consent_signed_at": w[3], "notes": w[4]}
    movement = [{"timestamp": s[0], "channel_name": s[1], "site_label": s[2], "confidence": s[3]} for s in sightings]
    field_reports = [
        {"timestamp": r[0], "raw_text": r[1], "parsed_fields": json.loads(r[2]) if r[2] else {}} for r in reports
    ]

    return templates.TemplateResponse(
        "worker_detail.html",
        {"request": request, "worker": worker, "movement": movement, "field_reports": field_reports},
    )
