"""Polls subscribed Telegram channels for new photo/text messages and queues
them in SQLite for the face-worker service to process.

Unlike earlier versions, Telegram API credentials, the session string, and
the channel watchlist all live in the app_settings table (set via the
dashboard's Admin -> Telegram / Admin -> Settings pages), not environment
variables. Until an admin connects Telegram through the web UI, this
service waits patiently rather than crashing.
"""
import time
import logging

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

from db import get_conn, get_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("listener")


def load_config(conn):
    api_id = get_setting(conn, "TG_API_ID")
    api_hash = get_setting(conn, "TG_API_HASH", decrypt=True)
    session_string = get_setting(conn, "TG_SESSION_STRING", decrypt=True)
    channels_raw = get_setting(conn, "TG_CHANNELS", "")
    poll_interval = int(get_setting(conn, "POLL_INTERVAL_SECONDS", "60"))
    channel_idents = [c.strip() for c in channels_raw.split(",") if c.strip()]
    return api_id, api_hash, session_string, channel_idents, poll_interval


def ensure_channel_rows(conn, client, channel_idents):
    """Upsert each configured channel into the channels table, return
    {telegram_id: (db_id, last_message_id)}."""
    rows = {}
    for ident in channel_idents:
        entity = client.get_entity(ident)
        tg_id = entity.id
        name = getattr(entity, "title", None) or getattr(entity, "username", str(tg_id))
        conn.execute(
            """
            INSERT INTO channels (telegram_channel_id, name) VALUES (?, ?)
            ON CONFLICT(telegram_channel_id) DO UPDATE SET name = excluded.name
            """,
            (tg_id, name),
        )
        db_id, last_message_id = conn.execute(
            "SELECT id, last_message_id FROM channels WHERE telegram_channel_id = ?", (tg_id,)
        ).fetchone()
        rows[tg_id] = {"entity": entity, "db_id": db_id, "last_message_id": last_message_id, "name": name}
    conn.commit()
    return rows


def poll_once(conn, client, channels):
    for tg_id, info in channels.items():
        new_last_id = info["last_message_id"]
        for message in client.iter_messages(info["entity"], min_id=info["last_message_id"], reverse=True):
            photo_path = None
            if message.photo:
                photo_path = f"/data/incoming/{info['db_id']}_{message.id}.jpg"
                client.download_media(message, file=photo_path)

            if photo_path or message.text:
                conn.execute(
                    """
                    INSERT INTO raw_messages (channel_id, telegram_message_id, timestamp, caption, photo_path)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, telegram_message_id) DO NOTHING
                    """,
                    (info["db_id"], message.id, message.date.isoformat(), message.text, photo_path),
                )
            new_last_id = max(new_last_id, message.id)

        if new_last_id != info["last_message_id"]:
            conn.execute("UPDATE channels SET last_message_id = ? WHERE id = ?", (new_last_id, info["db_id"]))
            info["last_message_id"] = new_last_id
    conn.commit()


def main():
    conn = get_conn()
    client = None
    active_session_string = None

    while True:
        api_id, api_hash, session_string, channel_idents, poll_interval = load_config(conn)

        if not (api_id and api_hash and session_string and channel_idents):
            if client is not None:
                client.disconnect()
                client = None
                active_session_string = None
            log.info("Waiting for Telegram to be connected via the admin UI (Admin -> Telegram)...")
            time.sleep(10)
            continue

        if client is None or session_string != active_session_string:
            if client is not None:
                client.disconnect()
            client = TelegramClient(StringSession(session_string), int(api_id), api_hash)
            client.connect()
            active_session_string = session_string
            log.info("Telegram client (re)connected")

        try:
            channels = ensure_channel_rows(conn, client, channel_idents)
            log.info("Watching channels: %s", [c["name"] for c in channels.values()])
            poll_once(conn, client, channels)
        except FloodWaitError as e:
            log.warning("Flood wait, sleeping %ss", e.seconds)
            time.sleep(e.seconds)
            continue
        except Exception:
            log.exception("Error during poll, will retry next interval")
            conn.rollback()
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
