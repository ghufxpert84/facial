import json
import os

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import telegram_connect
from auth import (
    AuthRedirect,
    get_current_user,
    hash_password,
    login_user,
    logout_user,
    require_admin,
    require_login,
    verify_password,
)
from db import get_conn, get_secret_key, get_setting, set_setting

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=get_secret_key())
templates = Jinja2Templates(directory="templates")


@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, exc: AuthRedirect):
    return RedirectResponse(exc.location, status_code=303)


def _users_exist(conn) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


# --- setup / login / logout -------------------------------------------------


@app.get("/setup")
def setup_form(request: Request):
    conn = get_conn()
    try:
        if _users_exist(conn):
            return RedirectResponse("/login", status_code=303)
    finally:
        conn.close()
    return templates.TemplateResponse("setup.html", {"request": request, "user": None, "error": None})


@app.post("/setup")
def setup_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    try:
        if _users_exist(conn):
            return RedirectResponse("/login", status_code=303)
        if len(password) < 8:
            return templates.TemplateResponse(
                "setup.html", {"request": request, "user": None, "error": "Password must be at least 8 characters."}
            )
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            (username, hash_password(password)),
        )
        conn.commit()
        user_id = cur.lastrowid
    finally:
        conn.close()
    login_user(request, user_id)
    return RedirectResponse("/", status_code=303)


@app.get("/login")
def login_form(request: Request):
    conn = get_conn()
    try:
        if not _users_exist(conn):
            return RedirectResponse("/setup", status_code=303)
    finally:
        conn.close()
    if get_current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not verify_password(password, row[1]):
        return templates.TemplateResponse(
            "login.html", {"request": request, "user": None, "error": "Invalid username or password."}
        )
    login_user(request, row[0])
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


# --- dashboard ---------------------------------------------------------------


@app.get("/")
def worker_directory(request: Request, user: dict = Depends(require_login)):
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
    return templates.TemplateResponse("index.html", {"request": request, "user": user, "workers": workers})


@app.get("/workers/{worker_id}")
def worker_detail(worker_id: int, request: Request, user: dict = Depends(require_login)):
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
        {"request": request, "user": user, "worker": worker, "movement": movement, "field_reports": field_reports},
    )


# --- admin: users --------------------------------------------------------------


@app.get("/admin/users")
def admin_users(request: Request, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, username, role, created_at FROM users ORDER BY username").fetchall()
    finally:
        conn.close()
    users = [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in rows]
    return templates.TemplateResponse(
        "admin_users.html", {"request": request, "user": user, "users": users, "current_user": user, "error": None}
    )


@app.post("/admin/users/create")
def admin_users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    user: dict = Depends(require_admin),
):
    conn = get_conn()
    try:
        if role not in ("admin", "viewer"):
            raise HTTPException(status_code=400, detail="Invalid role")
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, hash_password(password), role),
            )
            conn.commit()
        except Exception:
            rows = conn.execute("SELECT id, username, role, created_at FROM users ORDER BY username").fetchall()
            users = [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in rows]
            return templates.TemplateResponse(
                "admin_users.html",
                {
                    "request": request,
                    "user": user,
                    "users": users,
                    "current_user": user,
                    "error": f"Could not create user '{username}' — username may already exist.",
                },
            )
    finally:
        conn.close()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_id}/role")
def admin_users_role(target_id: int, request: Request, role: str = Form(...), user: dict = Depends(require_admin)):
    if target_id == user["id"]:
        return RedirectResponse("/admin/users", status_code=303)
    if role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Invalid role")
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_id}/delete")
def admin_users_delete(target_id: int, user: dict = Depends(require_admin)):
    if target_id == user["id"]:
        return RedirectResponse("/admin/users", status_code=303)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/users", status_code=303)


# --- admin: settings -------------------------------------------------------------


@app.get("/admin/settings")
def admin_settings(request: Request, user: dict = Depends(require_admin), saved: bool = False):
    conn = get_conn()
    try:
        settings = {
            "TG_CHANNELS": get_setting(conn, "TG_CHANNELS", ""),
            "MATCH_THRESHOLD": get_setting(conn, "MATCH_THRESHOLD", "0.45"),
            "RETENTION_DAYS": get_setting(conn, "RETENTION_DAYS", "90"),
            "POLL_INTERVAL_SECONDS": get_setting(conn, "POLL_INTERVAL_SECONDS", "60"),
            "UNRECOGNIZED_RETENTION_HOURS": get_setting(conn, "UNRECOGNIZED_RETENTION_HOURS", "72"),
            "HISTORY_PULL_HOURS": get_setting(conn, "HISTORY_PULL_HOURS", "24"),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin_settings.html", {"request": request, "user": user, "settings": settings, "saved": saved}
    )


@app.post("/admin/settings")
def admin_settings_save(
    request: Request,
    tg_channels: str = Form(""),
    match_threshold: str = Form("0.45"),
    retention_days: str = Form("90"),
    poll_interval_seconds: str = Form("60"),
    unrecognized_retention_hours: str = Form("72"),
    history_pull_hours: str = Form("24"),
    user: dict = Depends(require_admin),
):
    conn = get_conn()
    try:
        set_setting(conn, "TG_CHANNELS", tg_channels)
        set_setting(conn, "MATCH_THRESHOLD", match_threshold)
        set_setting(conn, "RETENTION_DAYS", retention_days)
        set_setting(conn, "POLL_INTERVAL_SECONDS", poll_interval_seconds)
        set_setting(conn, "UNRECOGNIZED_RETENTION_HOURS", unrecognized_retention_hours)
        set_setting(conn, "HISTORY_PULL_HOURS", history_pull_hours)
    finally:
        conn.close()
    return RedirectResponse("/admin/settings?saved=true", status_code=303)


# --- admin: telegram connect wizard ----------------------------------------------


@app.get("/admin/telegram")
def admin_telegram(request: Request, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        connected = bool(get_setting(conn, "TG_SESSION_STRING", None, decrypt=True))
        settings = {"TG_API_ID": get_setting(conn, "TG_API_ID", "")}
    finally:
        conn.close()
    stage = request.session.get("tg_wizard_stage", "idle")
    return templates.TemplateResponse(
        "admin_telegram.html",
        {"request": request, "user": user, "connected": connected, "stage": stage, "settings": settings, "error": None},
    )


@app.post("/admin/telegram/send-code")
async def admin_telegram_send_code(
    request: Request,
    api_id: str = Form(...),
    api_hash: str = Form(...),
    phone: str = Form(...),
    user: dict = Depends(require_admin),
):
    try:
        token = await telegram_connect.start_login(int(api_id), api_hash, phone)
    except Exception as e:
        return templates.TemplateResponse(
            "admin_telegram.html",
            {
                "request": request,
                "user": user,
                "connected": False,
                "stage": "idle",
                "settings": {"TG_API_ID": api_id},
                "error": str(e),
            },
        )
    request.session["tg_wizard_token"] = token
    request.session["tg_wizard_stage"] = "awaiting_code"
    request.session["tg_wizard_api_id"] = api_id
    request.session["tg_wizard_api_hash"] = api_hash
    return RedirectResponse("/admin/telegram", status_code=303)


async def _finish_telegram_login(conn, request: Request, api_id: str, api_hash: str, session_string: str):
    set_setting(conn, "TG_API_ID", api_id)
    set_setting(conn, "TG_API_HASH", api_hash, encrypt=True)
    set_setting(conn, "TG_SESSION_STRING", session_string, encrypt=True)
    request.session.pop("tg_wizard_token", None)
    request.session.pop("tg_wizard_stage", None)
    request.session.pop("tg_wizard_api_id", None)
    request.session.pop("tg_wizard_api_hash", None)


@app.post("/admin/telegram/verify-code")
async def admin_telegram_verify_code(request: Request, code: str = Form(...), user: dict = Depends(require_admin)):
    token = request.session.get("tg_wizard_token")
    status, result = await telegram_connect.submit_code(token, code)
    if status == "need_password":
        request.session["tg_wizard_stage"] = "awaiting_password"
        return RedirectResponse("/admin/telegram", status_code=303)
    if status == "error":
        return templates.TemplateResponse(
            "admin_telegram.html",
            {"request": request, "user": user, "connected": False, "stage": "awaiting_code", "settings": {}, "error": result},
        )
    conn = get_conn()
    try:
        await _finish_telegram_login(
            conn, request, request.session.get("tg_wizard_api_id"), request.session.get("tg_wizard_api_hash"), result
        )
    finally:
        conn.close()
    return RedirectResponse("/admin/telegram", status_code=303)


@app.post("/admin/telegram/verify-password")
async def admin_telegram_verify_password(
    request: Request, password: str = Form(...), user: dict = Depends(require_admin)
):
    token = request.session.get("tg_wizard_token")
    status, result = await telegram_connect.submit_password(token, password)
    if status == "error":
        return templates.TemplateResponse(
            "admin_telegram.html",
            {
                "request": request,
                "user": user,
                "connected": False,
                "stage": "awaiting_password",
                "settings": {},
                "error": result,
            },
        )
    conn = get_conn()
    try:
        await _finish_telegram_login(
            conn, request, request.session.get("tg_wizard_api_id"), request.session.get("tg_wizard_api_hash"), result
        )
    finally:
        conn.close()
    return RedirectResponse("/admin/telegram", status_code=303)


# --- admin: channels --------------------------------------------------------------


@app.get("/admin/channels")
def admin_channels(request: Request, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, name, site_label FROM channels ORDER BY name").fetchall()
    finally:
        conn.close()
    channels = [{"id": r[0], "name": r[1], "site_label": r[2]} for r in rows]
    return templates.TemplateResponse("admin_channels.html", {"request": request, "user": user, "channels": channels})


@app.post("/admin/channels/{channel_id}/site-label")
def admin_channels_site_label(
    channel_id: int, request: Request, site_label: str = Form(""), user: dict = Depends(require_admin)
):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE channels SET site_label = ? WHERE id = ?", (site_label or None, channel_id)
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


# --- admin: unrecognized faces (review queue) ------------------------------------
#
# Faces that don't match an enrolled worker land here, not in a permanent
# "unknown persons" table. An admin must explicitly name (enroll) or
# dismiss each one; unreviewed entries auto-expire after
# UNRECOGNIZED_RETENTION_HOURS (set in Admin -> Settings).


@app.get("/admin/unrecognized")
def admin_unrecognized(request: Request, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT u.id, u.first_seen, u.last_seen, u.sightings_count, c.name, c.site_label
            FROM unrecognized_faces u JOIN channels c ON c.id = u.channel_id
            ORDER BY u.last_seen DESC
            """
        ).fetchall()
    finally:
        conn.close()
    candidates = [
        {
            "id": r[0],
            "first_seen": r[1],
            "last_seen": r[2],
            "sightings_count": r[3],
            "channel_name": r[4],
            "site_label": r[5],
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        "admin_unrecognized.html", {"request": request, "user": user, "candidates": candidates}
    )


@app.get("/admin/unrecognized/{candidate_id}/photo")
def admin_unrecognized_photo(candidate_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        row = conn.execute("SELECT crop_path FROM unrecognized_faces WHERE id = ?", (candidate_id,)).fetchone()
    finally:
        conn.close()
    if row is None or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(row[0], media_type="image/jpeg")


@app.get("/admin/unrecognized/{candidate_id}/enroll")
def admin_unrecognized_enroll_form(candidate_id: int, request: Request, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM unrecognized_faces WHERE id = ?", (candidate_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return templates.TemplateResponse(
        "admin_unrecognized_enroll.html",
        {"request": request, "user": user, "candidate_id": candidate_id, "error": None},
    )


@app.post("/admin/unrecognized/{candidate_id}/enroll")
def admin_unrecognized_enroll_submit(
    candidate_id: int,
    request: Request,
    name: str = Form(...),
    employee_id: str = Form(...),
    consent_date: str = Form(...),
    notes: str = Form(""),
    user: dict = Depends(require_admin),
):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT embedding, crop_path FROM unrecognized_faces WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Candidate not found")
        embedding, crop_path = row
        try:
            cur = conn.execute(
                "INSERT INTO workers (name, employee_id, consent_signed_at, notes) VALUES (?, ?, ?, ?)",
                (name, employee_id, consent_date, notes or None),
            )
        except Exception:
            return templates.TemplateResponse(
                "admin_unrecognized_enroll.html",
                {
                    "request": request,
                    "user": user,
                    "candidate_id": candidate_id,
                    "error": f"Could not create worker — employee ID '{employee_id}' may already be in use.",
                },
            )
        worker_id = cur.lastrowid
        conn.execute(
            "INSERT INTO worker_face_embeddings (worker_id, embedding, source_photo_ref) VALUES (?, ?, ?)",
            (worker_id, embedding, crop_path),
        )
        conn.execute("DELETE FROM unrecognized_faces WHERE id = ?", (candidate_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/unrecognized", status_code=303)


@app.post("/admin/unrecognized/{candidate_id}/dismiss")
def admin_unrecognized_dismiss(candidate_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        row = conn.execute("SELECT crop_path FROM unrecognized_faces WHERE id = ?", (candidate_id,)).fetchone()
        conn.execute("DELETE FROM unrecognized_faces WHERE id = ?", (candidate_id,))
        conn.commit()
    finally:
        conn.close()
    if row is not None:
        try:
            os.remove(row[0])
        except FileNotFoundError:
            pass
    return RedirectResponse("/admin/unrecognized", status_code=303)
