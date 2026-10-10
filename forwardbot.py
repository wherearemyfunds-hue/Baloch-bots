#!/usr/bin/env python3
"""
WTB Forward Bot - PERFECTED VERSION
==============================================
Forwards messages starting with "WTB" from SOURCE_GROUPS to TARGET_CHANNEL,
appending a footer with the sender's contact and "Always use Baloch Escrow".

Fixes:
  1. Secrets via environment variables (API_ID, API_HASH, TARGET_CHANNEL)
  2. Rotating log file (no runaway growth), absolute paths
  3. Fail-fast if session unauthorized (no hanging on input())
  4. FloodWait handling on send
  5. Graceful shutdown

Required env vars: API_ID, API_HASH, TARGET_CHANNEL
Optional: SOURCE_GROUPS (comma-separated; defaults below)
==============================================
"""

import asyncio
import html
import logging
import os
import re
import sys
import traceback
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerUser

# ==================== BASE DIR ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "forwardbot.log")
SESSION_PATH = os.path.join(BASE_DIR, "forwardbot")

# ==================== CONFIG ====================
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "").strip()
TARGET_CHANNEL = os.environ.get("TARGET_CHANNEL", "").strip()

_default_sources = [
    "OTCVietNamNo1",
    "otc_doubIetop",
    "owlotc",
    "baloch0TC",
    "GROSS_OTC_LINKS",
    "tvrn_otc",
    "Wop_Capital",
]
_env_sources = os.environ.get("SOURCE_GROUPS", "").strip()
SOURCE_GROUPS = [s.strip() for s in _env_sources.split(",") if s.strip()] or _default_sources

# Spam auto-delete: groups where messages from senders matching SPAM_NAME_PATTERN
# (in username or display name, case-insensitive) are deleted automatically.
# Requires the account to have delete-messages admin right in those groups.
_env_spam_groups = os.environ.get("SPAM_DELETE_GROUPS", "").strip()
SPAM_DELETE_GROUPS = [s.strip() for s in _env_spam_groups.split(",") if s.strip()] or ["baloch0TC"]
SPAM_NAME_PATTERN = re.compile(os.environ.get("SPAM_NAME_PATTERN", r"xxin"), re.IGNORECASE)

MATCH_RE = re.compile(r"^\s*WTB", re.IGNORECASE)
PROCESS_DELAY_SECONDS = 0.3

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - [%(levelname)s] - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger("ForwardBot")


async def strong_fetch_username(client, message):
    """Best-effort sender username/user_id resolution."""
    username, user_id = None, None
    try:
        sender = await message.get_sender()
        if sender:
            username = getattr(sender, "username", None)
            user_id = getattr(sender, "id", None)
    except Exception:
        pass
    if username:
        return username, user_id
    from_id = getattr(message, "from_id", None)
    possible_id = None
    if from_id is not None:
        possible_id = getattr(from_id, "user_id", None) if not isinstance(from_id, int) else from_id
    if possible_id:
        user_id = possible_id
        try:
            ent = await client.get_entity(PeerUser(possible_id))
            username = getattr(ent, "username", None)
            if username:
                return username, user_id
        except Exception:
            pass
    return username, user_id


async def build_footer(client, message):
    username, user_id = await strong_fetch_username(client, message)
    if username:
        dm_line = f"DM: @{username}"
    elif user_id:
        dm_line = f"DM: tg://user?id={user_id}"
    else:
        dm_line = "DM: Unknown"
    return f"{dm_line}\nAlways use Baloch Escrow"


async def safe_send(client, target, message, final_text):
    """Send with FloodWait backoff. Returns True on success."""
    for attempt in range(3):
        try:
            if message.media:
                await client.send_file(target, message.media, caption=final_text)
            else:
                await client.send_message(target, final_text)
            return True
        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}s (attempt {attempt + 1}/3)")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            logger.error(f"Send error: {e}")
            return False
    logger.error("Send failed after 3 attempts (flood limits)")
    return False


async def main():
    if not API_ID or not API_HASH:
        logger.critical("API_ID / API_HASH env vars missing. Set them and restart.")
        sys.exit(1)
    if not TARGET_CHANNEL:
        logger.critical("TARGET_CHANNEL env var missing. Set it and restart.")
        sys.exit(1)

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH, flood_sleep_threshold=60)
    await client.connect()
    if not await client.is_user_authorized():
        logger.critical(
            "Session not authorized. Re-login interactively once, then restart. Refusing to hang."
        )
        sys.exit(1)
    me = await client.get_me()
    logger.info(f"Logged in as @{getattr(me, 'username', me.id)}")

    try:
        target = await client.get_entity(TARGET_CHANNEL)
        logger.info(f"Target channel resolved: {TARGET_CHANNEL}")
    except Exception as e:
        logger.critical(f"Cannot resolve TARGET_CHANNEL '{TARGET_CHANNEL}': {e}. Fix the channel name.")
        sys.exit(1)

    sources = []
    for s in SOURCE_GROUPS:
        try:
            sources.append(await client.get_entity(s))
            logger.info(f"Watching source: {s}")
        except Exception:
            logger.warning(f"Cannot access source group: {s}")
    if not sources:
        logger.critical("No valid source groups. Exiting.")
        sys.exit(1)

    @client.on(events.NewMessage(chats=sources))
    async def handler(event):
        m = event.message
        text = m.message or m.text or m.raw_text or ""
        if not text or not MATCH_RE.match(text):
            return
        try:
            footer = await build_footer(client, m)
            final_text = f"{text.strip()}\n\n{footer}"
            ok = await safe_send(client, target, m, final_text)
            if ok:
                logger.info(f"Forwarded WTB message (id={m.id})")
        except Exception:
            logger.error(f"Handler error:\n{traceback.format_exc()}")
        await asyncio.sleep(PROCESS_DELAY_SECONDS)

    logger.info("FORWARD BOT IS NOW RUNNING")

    # ---- Spam auto-delete for configured groups ----
    spam_targets = []
    for s in SPAM_DELETE_GROUPS:
        try:
            spam_targets.append(await client.get_entity(s))
            logger.info(f"Spam-delete watching: {s}")
        except Exception:
            logger.warning(f"Cannot access spam-delete group: {s}")
    if spam_targets:
        @client.on(events.NewMessage(chats=spam_targets))
        async def spam_handler(event):
            m = event.message
            try:
                sender = await m.get_sender()
            except Exception:
                sender = None
            if not sender:
                return
            username = (getattr(sender, "username", "") or "")
            display = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}"
            if SPAM_NAME_PATTERN.search(username) or SPAM_NAME_PATTERN.search(display):
                try:
                    await client.delete_messages(event.chat_id, [m.id])
                    logger.info(f"Deleted spam from @{username or display.strip()} (msg {m.id})")
                except Exception as e:
                    logger.warning(f"Delete failed (need admin delete right): {type(e).__name__}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except SystemExit:
        raise
    except Exception:
        logger.error(f"Fatal error\n{traceback.format_exc()}")
        sys.exit(1)
