import bcrypt

from db import get_conn


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def login_user(request, user_id: int):
    request.session["user_id"] = user_id


def logout_user(request):
    request.session.clear()


class AuthRedirect(Exception):
    """Raised by require_login/require_admin; caught by an exception
    handler in app.py that turns it into a redirect response, since a
    FastAPI dependency can't return a different response type directly."""

    def __init__(self, location: str):
        self.location = location


def get_current_user(request):
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"id": row[0], "username": row[1], "role": row[2]}


def require_login(request):
    user = get_current_user(request)
    if user is None:
        raise AuthRedirect("/login")
    return user


def require_admin(request):
    user = require_login(request)
    if user["role"] != "admin":
        raise AuthRedirect("/")
    return user
