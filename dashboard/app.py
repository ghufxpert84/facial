import json
import os
from datetime import datetime, timedelta, timezone

import requests
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
from db import get_conn, get_or_create_branch, get_secret_key, get_setting, set_setting

GMT8 = timezone(timedelta(hours=8))


def format_gmt8(value):
    """Jinja filter: every timestamp in the database is stored in UTC (SQLite's
    datetime('now') and Python's datetime.now(timezone.utc) both produce UTC,
    just in slightly different string formats) -- this converts either format
    to GMT+8 for display, without touching how anything is stored."""
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(GMT8).strftime("%Y-%m-%d %H:%M:%S")


def _apply_captured_info_to_branch(conn, branch_id, about):
    """Best-effort Key: Value extraction from a channel's captured About
    text into a branch's structured fields -- only fills fields still
    empty, never overwrites a manual edit. Mirrors the same helper in
    telegram-listener/listener.py (duplicated rather than shared, since
    each service is a separate Docker build context)."""
    conn.execute(
        "UPDATE branches SET captured_info = ? WHERE id = ? AND (captured_info IS NULL OR captured_info = '')",
        (about, branch_id),
    )
    for line in about.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not value:
            continue
        if "address" in key:
            conn.execute(
                "UPDATE branches SET address = ? WHERE id = ? AND (address IS NULL OR address = '')",
                (value, branch_id),
            )
        elif "wechat" in key:
            conn.execute(
                "UPDATE branches SET wechat_contact = ? WHERE id = ? AND (wechat_contact IS NULL OR wechat_contact = '')",
                (value, branch_id),
            )
        elif "telegram" in key:
            conn.execute(
                "UPDATE branches SET telegram_contact = ? WHERE id = ? AND (telegram_contact IS NULL OR telegram_contact = '')",
                (value, branch_id),
            )


def _geocode_address(conn, address):
    """Best-effort address -> (lat, lon) lookup for the branch map. Uses
    OpenStreetMap's free Nominatim service by default (no API key needed,
    but rate-limited to ~1 req/sec and requires a descriptive User-Agent
    per its usage policy -- fine for a manually-triggered single lookup).
    If an admin has set GEOCODE_PROVIDER=locationiq with a GEOCODE_API_KEY
    in Admin -> Settings, uses that instead for higher-volume/more reliable
    lookups. Returns None if nothing could be resolved."""
    provider = get_setting(conn, "GEOCODE_PROVIDER", "nominatim")
    try:
        if provider == "locationiq":
            api_key = get_setting(conn, "GEOCODE_API_KEY", None, decrypt=True)
            if not api_key:
                return None
            resp = requests.get(
                "https://us1.locationiq.com/v1/search",
                params={"key": api_key, "q": address, "format": "json", "limit": 1},
                timeout=10,
            )
        else:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "limit": 1},
                headers={"User-Agent": "telegram-worker-tracker/1.0 (self-hosted)"},
                timeout=10,
            )
        resp.raise_for_status()
        results = resp.json()
    except Exception:
        return None
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=get_secret_key())
templates = Jinja2Templates(directory="templates")
templates.env.filters["gmt8"] = format_gmt8


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
def worker_directory(request: Request, user: dict = Depends(require_login), branch: str = ""):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT w.id, w.name, w.employee_id, ranked.timestamp, c.name, c.site_label,
                   EXISTS(SELECT 1 FROM worker_face_embeddings e WHERE e.worker_id = w.id) AS has_avatar
            FROM workers w
            LEFT JOIN (
                SELECT worker_id, timestamp, channel_id,
                       ROW_NUMBER() OVER (PARTITION BY worker_id ORDER BY timestamp DESC, id DESC) AS rn
                FROM sightings
            ) ranked ON ranked.worker_id = w.id AND ranked.rn = 1
            LEFT JOIN channels c ON c.id = ranked.channel_id
            WHERE (? = '' OR c.site_label = ?)
            ORDER BY w.name
            """,
            (branch, branch),
        ).fetchall()
        branches = [r[0] for r in conn.execute("SELECT name FROM branches ORDER BY name").fetchall()]
        pending_review_count = conn.execute("SELECT COUNT(*) FROM unrecognized_faces").fetchone()[0]
        seen_today_count = conn.execute(
            "SELECT COUNT(DISTINCT worker_id) FROM sightings WHERE date(timestamp) = date('now')"
        ).fetchone()[0]
    finally:
        conn.close()

    workers = [
        {
            "id": r[0],
            "name": r[1],
            "employee_id": r[2],
            "last_seen": r[3],
            "channel_name": r[4],
            "site_label": r[5],
            "has_avatar": bool(r[6]),
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "workers": workers,
            "branches": branches,
            "selected_branch": branch,
            "pending_review_count": pending_review_count,
            "seen_today_count": seen_today_count,
        },
    )


@app.get("/map")
def branches_map(request: Request, user: dict = Depends(require_login)):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT b.id, b.name, b.address, b.map_url, b.telegram_contact, b.wechat_contact,
                   b.latitude, b.longitude,
                   (
                       SELECT COUNT(DISTINCT ranked.worker_id)
                       FROM (
                           SELECT worker_id, channel_id,
                                  ROW_NUMBER() OVER (PARTITION BY worker_id ORDER BY timestamp DESC, id DESC) AS rn
                           FROM sightings
                       ) ranked
                       JOIN channels c ON c.id = ranked.channel_id
                       WHERE ranked.rn = 1 AND c.branch_id = b.id
                   ) AS worker_count
            FROM branches b
            WHERE b.latitude IS NOT NULL AND b.longitude IS NOT NULL
            ORDER BY b.name
            """
        ).fetchall()
        unplaced_count = conn.execute(
            "SELECT COUNT(*) FROM branches WHERE latitude IS NULL OR longitude IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    branches = [
        {
            "id": r[0],
            "name": r[1],
            "address": r[2],
            "map_url": r[3],
            "telegram_contact": r[4],
            "wechat_contact": r[5],
            "latitude": r[6],
            "longitude": r[7],
            "worker_count": r[8],
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        "map.html",
        {"request": request, "user": user, "branches": branches, "unplaced_count": unplaced_count},
    )


@app.get("/workers/{worker_id}/avatar")
def worker_avatar(worker_id: int, user: dict = Depends(require_login)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT source_photo_ref FROM worker_face_embeddings WHERE worker_id = ? ORDER BY created_at DESC LIMIT 1",
            (worker_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row[0] or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="No avatar available")
    return FileResponse(row[0], media_type="image/jpeg")


@app.get("/workers/{worker_id}")
def worker_detail(worker_id: int, request: Request, user: dict = Depends(require_login)):
    conn = get_conn()
    try:
        w = conn.execute(
            """
            SELECT id, name, employee_id, consent_signed_at, notes,
                   EXISTS(SELECT 1 FROM worker_face_embeddings e WHERE e.worker_id = workers.id)
            FROM workers WHERE id = ?
            """,
            (worker_id,),
        ).fetchone()
        if w is None:
            raise HTTPException(status_code=404, detail="Worker not found")

        sightings = conn.execute(
            """
            SELECT s.id, s.timestamp, c.name, c.site_label, s.confidence, s.photo_path, s.video_path, c.branch_id
            FROM sightings s JOIN channels c ON c.id = s.channel_id
            WHERE s.worker_id = ? ORDER BY s.timestamp DESC, s.id DESC
            """,
            (worker_id,),
        ).fetchall()

        embeddings = conn.execute(
            "SELECT id, created_at FROM worker_face_embeddings WHERE worker_id = ? ORDER BY created_at DESC",
            (worker_id,),
        ).fetchall()

        reports = conn.execute(
            "SELECT timestamp, raw_text, parsed_fields FROM field_reports WHERE worker_id = ? ORDER BY timestamp DESC",
            (worker_id,),
        ).fetchall()

        current_branch = None
        if sightings:
            current_branch_id = sightings[0][7]
            if current_branch_id is not None:
                b = conn.execute(
                    "SELECT name, address, map_url, telegram_contact, wechat_contact, latitude, longitude "
                    "FROM branches WHERE id = ?",
                    (current_branch_id,),
                ).fetchone()
                if b is not None:
                    current_branch = {
                        "name": b[0],
                        "address": b[1],
                        "map_url": b[2],
                        "telegram_contact": b[3],
                        "wechat_contact": b[4],
                        "latitude": b[5],
                        "longitude": b[6],
                    }
    finally:
        conn.close()

    worker = {
        "id": w[0],
        "name": w[1],
        "employee_id": w[2],
        "consent_signed_at": w[3],
        "notes": w[4],
        "has_avatar": bool(w[5]),
    }
    movement = [
        {
            "sighting_id": s[0],
            "timestamp": s[1],
            "channel_name": s[2],
            "site_label": s[3],
            "confidence": s[4],
            "has_photo": bool(s[5]),
            "has_video": bool(s[6]),
        }
        for s in sightings
    ]
    reference_photos = [{"id": e[0], "created_at": e[1]} for e in embeddings]
    gallery_count = len(reference_photos) + sum(1 for m in movement if m["has_photo"])
    field_reports = [
        {"timestamp": r[0], "raw_text": r[1], "parsed_fields": json.loads(r[2]) if r[2] else {}} for r in reports
    ]

    return templates.TemplateResponse(
        "worker_detail.html",
        {
            "request": request,
            "user": user,
            "worker": worker,
            "movement": movement,
            "movement_chronological": list(reversed(movement)),
            "current_branch": current_branch,
            "reference_photos": reference_photos,
            "gallery_count": gallery_count,
            "field_reports": field_reports,
        },
    )


@app.get("/workers/{worker_id}/edit")
def worker_edit_form(worker_id: int, request: Request, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        w = conn.execute(
            "SELECT id, name, employee_id, consent_signed_at, notes FROM workers WHERE id = ?", (worker_id,)
        ).fetchone()
    finally:
        conn.close()
    if w is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    worker = {"id": w[0], "name": w[1], "employee_id": w[2], "consent_signed_at": w[3], "notes": w[4]}
    return templates.TemplateResponse(
        "worker_edit.html", {"request": request, "user": user, "worker": worker, "error": None}
    )


@app.post("/workers/{worker_id}/edit")
def worker_edit_submit(
    worker_id: int,
    request: Request,
    name: str = Form(...),
    employee_id: str = Form(...),
    consent_signed_at: str = Form(...),
    notes: str = Form(""),
    user: dict = Depends(require_admin),
):
    conn = get_conn()
    try:
        try:
            conn.execute(
                "UPDATE workers SET name = ?, employee_id = ?, consent_signed_at = ?, notes = ? WHERE id = ?",
                (name, employee_id, consent_signed_at, notes or None, worker_id),
            )
            conn.commit()
        except Exception:
            worker = {
                "id": worker_id,
                "name": name,
                "employee_id": employee_id,
                "consent_signed_at": consent_signed_at,
                "notes": notes,
            }
            return templates.TemplateResponse(
                "worker_edit.html",
                {
                    "request": request,
                    "user": user,
                    "worker": worker,
                    "error": f"Could not save — employee ID '{employee_id}' may already be in use by another worker.",
                },
            )
    finally:
        conn.close()
    return RedirectResponse(f"/workers/{worker_id}", status_code=303)


@app.post("/workers/{worker_id}/delete")
def worker_delete(worker_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/", status_code=303)


@app.get("/workers/{worker_id}/photo/{sighting_id}")
def worker_sighting_photo(worker_id: int, sighting_id: int, user: dict = Depends(require_login)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT photo_path FROM sightings WHERE id = ? AND worker_id = ?", (sighting_id, worker_id)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row[0] or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(row[0], media_type="image/jpeg")


@app.get("/workers/{worker_id}/video/{sighting_id}")
def worker_sighting_video(worker_id: int, sighting_id: int, user: dict = Depends(require_login)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT video_path FROM sightings WHERE id = ? AND worker_id = ?", (sighting_id, worker_id)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row[0] or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(row[0], media_type="video/mp4")


@app.get("/workers/{worker_id}/reference-photo/{embedding_id}")
def worker_reference_photo(worker_id: int, embedding_id: int, user: dict = Depends(require_login)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT source_photo_ref FROM worker_face_embeddings WHERE id = ? AND worker_id = ?",
            (embedding_id, worker_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row[0] or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(row[0], media_type="image/jpeg")


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
            "MATCH_THRESHOLD": get_setting(conn, "MATCH_THRESHOLD", "0.45"),
            "RETENTION_DAYS": get_setting(conn, "RETENTION_DAYS", "90"),
            "POLL_INTERVAL_SECONDS": get_setting(conn, "POLL_INTERVAL_SECONDS", "60"),
            "UNRECOGNIZED_RETENTION_HOURS": get_setting(conn, "UNRECOGNIZED_RETENTION_HOURS", "72"),
            "HISTORY_PULL_HOURS": get_setting(conn, "HISTORY_PULL_HOURS", "24"),
            "GEOCODE_PROVIDER": get_setting(conn, "GEOCODE_PROVIDER", "nominatim"),
            "GEOCODE_API_KEY_SET": bool(get_setting(conn, "GEOCODE_API_KEY", None, decrypt=True)),
        }
    finally:
        conn.close()
    return templates.TemplateResponse(
        "admin_settings.html", {"request": request, "user": user, "settings": settings, "saved": saved}
    )


@app.post("/admin/settings")
def admin_settings_save(
    request: Request,
    match_threshold: str = Form("0.45"),
    retention_days: str = Form("90"),
    poll_interval_seconds: str = Form("60"),
    unrecognized_retention_hours: str = Form("72"),
    history_pull_hours: str = Form("24"),
    geocode_provider: str = Form("nominatim"),
    geocode_api_key: str = Form(""),
    user: dict = Depends(require_admin),
):
    conn = get_conn()
    try:
        set_setting(conn, "MATCH_THRESHOLD", match_threshold)
        set_setting(conn, "RETENTION_DAYS", retention_days)
        set_setting(conn, "POLL_INTERVAL_SECONDS", poll_interval_seconds)
        set_setting(conn, "UNRECOGNIZED_RETENTION_HOURS", unrecognized_retention_hours)
        set_setting(conn, "HISTORY_PULL_HOURS", history_pull_hours)
        set_setting(conn, "GEOCODE_PROVIDER", geocode_provider)
        if geocode_api_key.strip():
            set_setting(conn, "GEOCODE_API_KEY", geocode_api_key.strip(), encrypt=True)
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
        rows = conn.execute(
            """
            SELECT wl.id, wl.identifier, wl.enabled,
                   c.id, c.name, c.site_label, c.last_polled_at, c.last_message_id, c.latest_known_message_id,
                   (SELECT COUNT(*) FROM raw_messages rm WHERE rm.channel_id = c.id AND rm.processed_at IS NULL) AS pending_count
            FROM channel_watchlist wl
            LEFT JOIN channels c ON c.identifier = wl.identifier
            ORDER BY wl.identifier
            """
        ).fetchall()
    finally:
        conn.close()

    channels = []
    for r in rows:
        channel_id, last_message_id, latest_known_message_id, pending_count = r[3], r[7], r[8], r[9]
        progress_percent = None
        if latest_known_message_id and latest_known_message_id > 0:
            progress_percent = min(100, round(last_message_id / latest_known_message_id * 100))
        channels.append(
            {
                "watchlist_id": r[0],
                "identifier": r[1],
                "enabled": bool(r[2]),
                "channel_id": channel_id,
                "name": r[4] or r[1],
                "site_label": r[5],
                "last_polled_at": r[6],
                "last_message_id": last_message_id,
                "latest_known_message_id": latest_known_message_id,
                "progress_percent": progress_percent,
                "pending_count": pending_count or 0,
                "resolved": channel_id is not None,
            }
        )
    return templates.TemplateResponse("admin_channels.html", {"request": request, "user": user, "channels": channels})


@app.post("/admin/channels/add")
def admin_channels_add(request: Request, identifiers: str = Form(...), user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        for line in identifiers.splitlines():
            ident = line.strip()
            if ident:
                conn.execute(
                    "INSERT INTO channel_watchlist (identifier) VALUES (?) ON CONFLICT(identifier) DO NOTHING",
                    (ident,),
                )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@app.post("/admin/channels/watchlist/{watchlist_id}/toggle")
def admin_channels_toggle(watchlist_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE channel_watchlist SET enabled = 1 - enabled WHERE id = ?", (watchlist_id,)
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@app.post("/admin/channels/watchlist/{watchlist_id}/remove")
def admin_channels_remove(watchlist_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM channel_watchlist WHERE id = ?", (watchlist_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@app.post("/admin/channels/{channel_id}/skip-to-latest")
def admin_channels_skip_to_latest(channel_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        conn.execute("UPDATE channels SET skip_to_latest = 1 WHERE id = ?", (channel_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@app.post("/admin/channels/{channel_id}/reset-scan")
def admin_channels_reset_scan(channel_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        # Zero last_message_id here immediately (not waiting for
        # telegram-listener's next poll cycle to notice reset_scan=1) so the
        # progress bar drops to 0% right away instead of sitting stale at
        # its old value until the listener catches up.
        conn.execute("UPDATE channels SET reset_scan = 1, last_message_id = 0 WHERE id = ?", (channel_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


@app.get("/admin/channels/{channel_id}/progress")
def admin_channels_progress(channel_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT last_message_id, latest_known_message_id, last_polled_at FROM channels WHERE id = ?",
            (channel_id,),
        ).fetchone()
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM raw_messages WHERE channel_id = ? AND processed_at IS NULL", (channel_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    last_message_id, latest_known_message_id, last_polled_at = row
    progress_percent = None
    if latest_known_message_id and latest_known_message_id > 0:
        progress_percent = min(100, round(last_message_id / latest_known_message_id * 100))
    return {
        "last_message_id": last_message_id,
        "latest_known_message_id": latest_known_message_id,
        "progress_percent": progress_percent,
        "pending_count": pending_count,
        "last_polled_at": format_gmt8(last_polled_at) if last_polled_at else None,
    }


@app.post("/admin/channels/{channel_id}/site-label")
def admin_channels_site_label(
    channel_id: int, request: Request, site_label: str = Form(""), user: dict = Depends(require_admin)
):
    conn = get_conn()
    try:
        site_label = site_label.strip() or None
        conn.execute("UPDATE channels SET site_label = ? WHERE id = ?", (site_label, channel_id))
        if site_label is None:
            conn.execute("UPDATE channels SET branch_id = NULL WHERE id = ?", (channel_id,))
        else:
            branch_id = get_or_create_branch(conn, site_label)
            conn.execute("UPDATE channels SET branch_id = ? WHERE id = ?", (branch_id, channel_id))
            captured_info = conn.execute(
                "SELECT captured_info FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()[0]
            if captured_info:
                _apply_captured_info_to_branch(conn, branch_id, captured_info)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/channels", status_code=303)


# --- admin: branches ---------------------------------------------------------------
#
# A branch's name is kept in sync with its channel's site_label (created/
# linked from admin_channels_site_label above) -- Branches isn't where you
# name a branch, it's where you fill in the extra details for one that
# already exists.


@app.get("/admin/branches")
def admin_branches(request: Request, user: dict = Depends(require_admin), geocode: str = ""):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT b.id, b.name, b.address, b.map_url, b.telegram_contact, b.wechat_contact, b.captured_info,
                   b.latitude, b.longitude,
                   (SELECT COUNT(*) FROM channels c WHERE c.branch_id = b.id) AS channel_count
            FROM branches b
            ORDER BY b.name
            """
        ).fetchall()
    finally:
        conn.close()
    branches = [
        {
            "id": r[0],
            "name": r[1],
            "address": r[2],
            "map_url": r[3],
            "telegram_contact": r[4],
            "wechat_contact": r[5],
            "captured_info": r[6],
            "latitude": r[7],
            "longitude": r[8],
            "channel_count": r[9],
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        "admin_branches.html", {"request": request, "user": user, "branches": branches, "geocode": geocode}
    )


@app.post("/admin/branches/{branch_id}")
def admin_branches_save(
    branch_id: int,
    request: Request,
    address: str = Form(""),
    map_url: str = Form(""),
    telegram_contact: str = Form(""),
    wechat_contact: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    user: dict = Depends(require_admin),
):
    try:
        lat_value = float(latitude) if latitude.strip() else None
        lon_value = float(longitude) if longitude.strip() else None
    except ValueError:
        return RedirectResponse("/admin/branches?geocode=bad_coords", status_code=303)

    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE branches
            SET address = ?, map_url = ?, telegram_contact = ?, wechat_contact = ?, latitude = ?, longitude = ?
            WHERE id = ?
            """,
            (
                address or None,
                map_url or None,
                telegram_contact or None,
                wechat_contact or None,
                lat_value,
                lon_value,
                branch_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/branches", status_code=303)


@app.post("/admin/branches/{branch_id}/geocode")
def admin_branches_geocode(branch_id: int, user: dict = Depends(require_admin)):
    conn = get_conn()
    try:
        row = conn.execute("SELECT address FROM branches WHERE id = ?", (branch_id,)).fetchone()
        if row is None or not row[0]:
            return RedirectResponse("/admin/branches?geocode=no_address", status_code=303)
        coords = _geocode_address(conn, row[0])
        if coords is None:
            return RedirectResponse("/admin/branches?geocode=failed", status_code=303)
        conn.execute(
            "UPDATE branches SET latitude = ?, longitude = ? WHERE id = ?", (coords[0], coords[1], branch_id)
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/admin/branches?geocode=ok", status_code=303)


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


# --- admin: logs -------------------------------------------------------------


@app.get("/admin/logs")
def admin_logs(request: Request, user: dict = Depends(require_admin), service: str = ""):
    conn = get_conn()
    try:
        if service:
            rows = conn.execute(
                "SELECT service, level, message, created_at FROM logs WHERE service = ? ORDER BY id DESC LIMIT 200",
                (service,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT service, level, message, created_at FROM logs ORDER BY id DESC LIMIT 200"
            ).fetchall()
        services = [r[0] for r in conn.execute("SELECT DISTINCT service FROM logs ORDER BY service").fetchall()]
    finally:
        conn.close()
    logs = [{"service": r[0], "level": r[1], "message": r[2], "created_at": r[3]} for r in rows]
    return templates.TemplateResponse(
        "admin_logs.html",
        {"request": request, "user": user, "logs": logs, "services": services, "selected_service": service},
    )
