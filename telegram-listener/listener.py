"""Polls subscribed Telegram channels for new photo/text messages and queues
them in Postgres for the face-worker service to process.

Channels to watch are given via TG_CHANNELS (comma-separated usernames or
numeric ids), e.g. "somefieldreports,-1001234567890".
"""
import os
import time
import logging

import psycopg2
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("listener")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ.get("TG_SESSION_STRING", "")
DATABASE_URL = os.environ["DATABASE_URL"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
CHANNELS = [c.strip() for c in os.environ.get("TG_CHANNELS", "").split(",") if c.strip()]

if not SESSION_STRING:
    raise SystemExit(
        "TG_SESSION_STRING is not set. Run `docker compose run --rm "
        "telegram-listener python login.py` once and put the result in .env."
    )
if not CHANNELS:
    raise SystemExit("TG_CHANNELS is not set — list at least one channel to monitor.")


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def ensure_channel_rows(conn, client):
    """Upsert each configured channel into the channels table, return
    {telegram_id: (db_id, last_message_id)}."""
    rows = {}
    with conn.cursor() as cur:
        for ident in CHANNELS:
            entity = client.get_entity(ident)
            tg_id = entity.id
            name = getattr(entity, "title", None) or getattr(entity, "username", str(tg_id))
            cur.execute(
                """
                INSERT INTO channels (telegram_channel_id, name)
                VALUES (%s, %s)
                ON CONFLICT (telegram_channel_id) DO UPDATE SET name = EXCLUDED.name
                RETURNING id, last_message_id
                """,
                (tg_id, name),
            )
            db_id, last_message_id = cur.fetchone()
            rows[tg_id] = {"entity": entity, "db_id": db_id, "last_message_id": last_message_id, "name": name}
    conn.commit()
    return rows


def poll_once(conn, client, channels):
    with conn.cursor() as cur:
        for tg_id, info in channels.items():
            new_last_id = info["last_message_id"]
            for message in client.iter_messages(info["entity"], min_id=info["last_message_id"], reverse=True):
                photo_path = None
                if message.photo:
                    photo_path = f"/data/incoming/{info['db_id']}_{message.id}.jpg"
                    client.download_media(message, file=photo_path)

                if photo_path or message.text:
                    cur.execute(
                        """
                        INSERT INTO raw_messages (channel_id, telegram_message_id, timestamp, caption, photo_path)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (channel_id, telegram_message_id) DO NOTHING
                        """,
                        (info["db_id"], message.id, message.date, message.text, photo_path),
                    )
                new_last_id = max(new_last_id, message.id)

            if new_last_id != info["last_message_id"]:
                cur.execute("UPDATE channels SET last_message_id = %s WHERE id = %s", (new_last_id, info["db_id"]))
                info["last_message_id"] = new_last_id
    conn.commit()


def main():
    conn = get_conn()
    with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        channels = ensure_channel_rows(conn, client)
        log.info("Watching channels: %s", [c["name"] for c in channels.values()])
        while True:
            try:
                poll_once(conn, client, channels)
            except FloodWaitError as e:
                log.warning("Flood wait, sleeping %ss", e.seconds)
                time.sleep(e.seconds)
                continue
            except Exception:
                log.exception("Error during poll, will retry next interval")
                conn.rollback()
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
