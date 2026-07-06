"""Polls subscribed Telegram channels for new photo/video/text messages and
queues them in SQLite for the face-worker service to process.

Unlike earlier versions, Telegram API credentials, the session string, and
the channel watchlist all live in the app_settings table (set via the
dashboard's Admin -> Telegram / Admin -> Settings pages), not environment
variables. Until an admin connects Telegram through the web UI, this
service waits patiently rather than crashing.
"""
import time
import logging
from datetime import datetime, timedelta, timezone

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

from db import get_conn, get_setting, log_event, purge_old_logs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("listener")
SERVICE_NAME = "telegram-listener"


def load_config(conn):
    api_id = get_setting(conn, "TG_API_ID")
    api_hash = get_setting(conn, "TG_API_HASH", decrypt=True)
    session_string = get_setting(conn, "TG_SESSION_STRING", decrypt=True)
    channels_raw = get_setting(conn, "TG_CHANNELS", "")
    poll_interval = int(get_setting(conn, "POLL_INTERVAL_SECONDS", "60"))
    history_pull_hours = int(get_setting(conn, "HISTORY_PULL_HOURS", "24"))
    channel_idents = [c.strip() for c in channels_raw.split(",") if c.strip()]
    return api_id, api_hash, session_string, channel_idents, poll_interval, history_pull_hours


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
        db_id, last_message_id, skip_to_latest = conn.execute(
            "SELECT id, last_message_id, skip_to_latest FROM channels WHERE telegram_channel_id = ?", (tg_id,)
        ).fetchone()
        rows[tg_id] = {
            "entity": entity,
            "db_id": db_id,
            "last_message_id": last_message_id,
            "name": name,
            "skip_to_latest": bool(skip_to_latest),
        }
    conn.commit()
    return rows


def poll_once(conn, client, channels, history_pull_hours):
    """Processes messages oldest-first per channel, committing after each
    one. This matters for large backlogs: if a single photo download times
    out partway through (Telegram's file servers can choke on a burst of
    many requests in a row, e.g. catching up on hours of history at once),
    only that message is retried next cycle -- everything processed before
    it stays committed instead of being rolled back with it.

    A channel's very first poll (last_message_id still 0) only reaches back
    HISTORY_PULL_HOURS instead of pulling the entire channel history --
    both to avoid exactly that kind of download burst on a newly-added
    channel, and because old history from before the system was connected
    isn't useful for tracking current worker location anyway.
    """
    for tg_id, info in channels.items():
        if info["skip_to_latest"]:
            latest = client.get_messages(info["entity"], limit=1)
            new_last_id = latest[0].id if latest else info["last_message_id"]
            conn.execute(
                "UPDATE channels SET last_message_id = ?, skip_to_latest = 0, last_polled_at = ? WHERE id = ?",
                (new_last_id, datetime.now(timezone.utc).isoformat(), info["db_id"]),
            )
            conn.commit()
            info["last_message_id"] = new_last_id
            log.info("Skipped remaining backlog for %s, resuming from message %s", info["name"], new_last_id)
            log_event(
                conn,
                SERVICE_NAME,
                "info",
                f"Skipped remaining backlog for {info['name']} at admin's request, resuming from message {new_last_id}",
            )
            continue  # nothing to process this cycle -- we just fast-forwarded past it, last_polled_at already set above

        if info["last_message_id"] == 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=history_pull_hours)
            message_iter = client.iter_messages(info["entity"], offset_date=cutoff, reverse=True)
        else:
            message_iter = client.iter_messages(info["entity"], min_id=info["last_message_id"], reverse=True)

        for message in message_iter:
            photo_path = None
            video_path = None
            media_target = None
            if message.photo:
                photo_path = f"/data/incoming/{info['db_id']}_{message.id}.jpg"
                media_target = photo_path
            elif message.video:
                video_path = f"/data/incoming/{info['db_id']}_{message.id}.mp4"
                media_target = video_path

            if media_target:
                try:
                    client.download_media(message, file=media_target)
                    time.sleep(0.5)  # avoid bursting many file requests in a row
                except Exception as e:
                    log.warning(
                        "Failed to download media for message %s in %s, will retry next cycle",
                        message.id,
                        info["name"],
                        exc_info=True,
                    )
                    log_event(
                        conn,
                        SERVICE_NAME,
                        "warning",
                        f"Failed to download media for message {message.id} in {info['name']}: {e} -- will retry next cycle",
                    )
                    break  # stop this channel for this cycle; other channels still get processed

            if photo_path or video_path or message.text:
                conn.execute(
                    """
                    INSERT INTO raw_messages (channel_id, telegram_message_id, timestamp, caption, photo_path, video_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, telegram_message_id) DO NOTHING
                    """,
                    (info["db_id"], message.id, message.date.isoformat(), message.text, photo_path, video_path),
                )
            conn.execute("UPDATE channels SET last_message_id = ? WHERE id = ?", (message.id, info["db_id"]))
            conn.commit()
            info["last_message_id"] = message.id

        # Record that this channel was actually scanned this cycle, whether
        # or not any new messages were found -- this is what lets the
        # dashboard show "last checked N seconds ago" per channel.
        conn.execute(
            "UPDATE channels SET last_polled_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), info["db_id"]),
        )
        conn.commit()


def main():
    conn = get_conn()
    client = None
    active_session_string = None
    was_waiting = False

    while True:
        api_id, api_hash, session_string, channel_idents, poll_interval, history_pull_hours = load_config(conn)

        if not (api_id and api_hash and session_string and channel_idents):
            if client is not None:
                client.disconnect()
                client = None
                active_session_string = None
            log.info("Waiting for Telegram to be connected via the admin UI (Admin -> Telegram)...")
            if not was_waiting:
                log_event(
                    conn, SERVICE_NAME, "warning", "Waiting for Telegram to be connected via the admin UI"
                )
                was_waiting = True
            time.sleep(10)
            continue
        was_waiting = False

        if client is None or session_string != active_session_string:
            if client is not None:
                client.disconnect()
            client = TelegramClient(StringSession(session_string), int(api_id), api_hash)
            client.connect()
            active_session_string = session_string
            log.info("Telegram client (re)connected")
            log_event(conn, SERVICE_NAME, "info", "Telegram client (re)connected")

        try:
            channels = ensure_channel_rows(conn, client, channel_idents)
            log.info("Watching channels: %s", [c["name"] for c in channels.values()])
            poll_once(conn, client, channels, history_pull_hours)
            purge_old_logs(conn)
        except FloodWaitError as e:
            log.warning("Flood wait, sleeping %ss", e.seconds)
            log_event(conn, SERVICE_NAME, "warning", f"Flood wait, sleeping {e.seconds}s")
            time.sleep(e.seconds)
            continue
        except Exception as e:
            log.exception("Error during poll, will retry next interval")
            conn.rollback()
            log_event(conn, SERVICE_NAME, "error", f"Error during poll: {e}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
