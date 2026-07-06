"""One-time interactive login to produce a Telethon session string.

Run this once, outside the normal restart loop:
    docker compose run --rm telegram-listener python login.py

It will prompt for your phone number, the login code Telegram sends you, and
(if enabled) your 2FA password. Copy the printed session string into .env as
TG_SESSION_STRING, then start the stack normally.
"""
import os

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.environ["TG_API_ID"])
api_hash = os.environ["TG_API_HASH"]

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nSession string (copy this into .env as TG_SESSION_STRING):\n")
    print(client.session.save())
