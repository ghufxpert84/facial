"""Backend for the in-browser "Connect Telegram" wizard: phone number ->
login code -> optional 2FA password -> a working session string, without
ever touching a terminal or Portainer's Console.

Pending logins are held in memory only (single dashboard process/worker),
keyed by a random wizard token stored in the browser session.
"""
import secrets

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

_pending = {}


async def start_login(api_id: int, api_hash: str, phone: str) -> str:
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    token = secrets.token_urlsafe(16)
    _pending[token] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": sent.phone_code_hash,
    }
    return token


async def submit_code(token: str, code: str):
    """Returns (status, result): status is "success" (result = session
    string), "need_password" (2FA required, call submit_password next), or
    "error" (result = message)."""
    state = _pending.get(token)
    if state is None:
        return "error", "Login session expired — start again."
    client = state["client"]
    try:
        await client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
    except SessionPasswordNeededError:
        return "need_password", None
    except Exception as e:
        _pending.pop(token, None)
        return "error", str(e)

    session_string = client.session.save()
    await client.disconnect()
    _pending.pop(token, None)
    return "success", session_string


async def submit_password(token: str, password: str):
    state = _pending.get(token)
    if state is None:
        return "error", "Login session expired — start again."
    client = state["client"]
    try:
        await client.sign_in(password=password)
    except Exception as e:
        return "error", str(e)

    session_string = client.session.save()
    await client.disconnect()
    _pending.pop(token, None)
    return "success", session_string
