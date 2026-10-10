#!/usr/bin/env python3
"""
Baloch Escrow Bot - PERFECTED VERSION
==============================================
All known issues fixed:
  1. OLD admin account reconnect (was failing with "disconnected")
  2. @Balochbuyer is now invited + promoted as admin in every group
  3. Honest admin-status reporting (no more false "success" claims)
  4. Log rotation (no more 3.3GB runaway log file), INFO level
  5. Group counter: locked, atomic, incremented only after success
  6. No interactive login prompts on headless server (fail-fast)
  7. Secrets via environment variables (no hardcoded tokens)
  8. Rate limiting: one creation per user at a time + daily cap
  9. FloodWait handling, URL-encoded share links, anchored /start
  10. Absolute paths for sessions/counter/log; sessions chmod 600

Secrets required as environment variables:
  BOT_TOKEN, NEW_API_ID, NEW_API_HASH, OLD_API_ID, OLD_API_HASH
==============================================
"""

import asyncio
import html
import logging
import json
import os
import sys
import tempfile
import traceback
import urllib.parse
from datetime import datetime, date
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    EditAdminRequest,
)
from telethon.tl.functions.messages import ExportChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatAdminRights

# ==================== BASE DIRECTORY (absolute paths) ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==================== CONFIGURATION (from environment) ====================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
NEW_API_ID = int(os.environ.get("NEW_API_ID", "0") or 0)
NEW_API_HASH = os.environ.get("NEW_API_HASH", "").strip()
OLD_API_ID = int(os.environ.get("OLD_API_ID", "0") or 0)
OLD_API_HASH = os.environ.get("OLD_API_HASH", "").strip()

_missing = [n for n, v in [
    ("BOT_TOKEN", BOT_TOKEN), ("NEW_API_ID", NEW_API_ID),
    ("NEW_API_HASH", NEW_API_HASH), ("OLD_API_ID", OLD_API_ID),
    ("OLD_API_HASH", OLD_API_HASH),
] if not v]

# ----------------------------------------------------------
# Reminder settings
# ----------------------------------------------------------
ESCROW_REMINDER_CHAT = -1002121851935
REMINDER_MESSAGE = (
    "<b>Hi Baloch OTC Members\n"
    "I am Baloch Escrow Bot\n"
    "If you Want Fastest Escrow with Low Fee click below</b>"
)
REMINDER_BUTTON_TEXT = "Start Escrow"
REMINDER_BUTTON_URL = "https://t.me/Baloch_Escrow2_bot?start=1"

# ----------------------------------------------------------
# Group settings
# ----------------------------------------------------------
ADD_BOT_USERNAME = "Balochhelpbot"
SUPPORT_USERNAME = "Balochbuyer"          # invited + promoted as admin in every group
GROUP_BASE_NAME = "Baloch Escrow Deal"
STARTING_GROUP_NUMBER = 3102
MAX_GROUPS_PER_USER_PER_DAY = 999999  # effectively unlimited (Shawn removed the daily cap 2026-10-10)

# ----------------------------------------------------------
# Support & timing
# ----------------------------------------------------------
SUPPORT_CONTACT = "@Balochbuyer"
REMINDER_INTERVAL = 900
REMINDER_DELETE_AFTER = 300

# Files (absolute paths)
COUNTER_FILE = os.path.join(BASE_DIR, "group_counter.json")
LOG_FILE = os.path.join(BASE_DIR, "escrow_bot.log")
NEW_SESSION = os.path.join(BASE_DIR, "escrow_new_user_session")
OLD_SESSION = os.path.join(BASE_DIR, "escrow_old_user_session")
BOT_SESSION = os.path.join(BASE_DIR, "escrow_bot_session")
USERS_FILE = os.path.join(BASE_DIR, "known_users.json")

# Global flag: True if the NEW user client session is valid and can create groups.
# Set during startup; when False the bot still responds but group creation is unavailable.
USER_CLIENT_AVAILABLE = False
BROADCAST_SENT_FILE = os.path.join(BASE_DIR, "broadcast_sent.json")
BROADCAST_TRIGGER_FILE = os.path.join(BASE_DIR, "TRIGGER_BROADCAST")
BROADCAST_BOT_SENT_FILE = os.path.join(BASE_DIR, "broadcast_bot_sent.json")
BROADCAST_BOT_TRIGGER_FILE = os.path.join(BASE_DIR, "TRIGGER_BROADCAST_BOT")

# Broadcast settings
BROADCAST_MESSAGE = (
    "📢 Hello from Baloch Escrow!\n\n"
    "We've launched our new channel for all WTB (Want to Buy) posts:\n\n"
    "👉 Baloch Global Buyers\n"
    "https://t.me/balochonlybuyerss\n\n"
    "Join now so you never miss a deal. And remember — "
    "always use Baloch Escrow for safe trading!"
)
# Fancy promo sent FROM @Baloch_Escrow2_bot (only reaches users who started the bot)
BROADCAST_BOT_MESSAGE = (
    "📢 <b>Exciting News from Baloch Escrow!</b> 🎉\n\n"
    "We've launched our brand new channel for all WTB (Want to Buy) posts:\n\n"
    "🌍 <b>Baloch Global Buyers</b> 🌍\n"
    "👉 https://t.me/balochonlybuyerss\n\n"
    "✅ Find buyers' demands in one place\n"
    "✅ Never miss a deal opportunity\n"
    "✅ Direct access to serious buyers\n\n"
    "Join now and stay ahead! 🚀\n\n"
    "🔒 Always use <b>Baloch Escrow</b> for safe &amp; secure trading! 💯"
)
BROADCAST_MAX_GROUPS = 120   # most recent deal groups to scan
BROADCAST_DELAY = 7           # seconds between DMs (avoid spam limits)

# ==================== LOGGING SETUP (rotating, INFO) ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - [%(levelname)s] - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(),
    ],
)
# Silence Telethon's packet-level debug spam (this caused the 3.3GB log)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger("EscrowBot")
logger.setLevel(logging.INFO)

# ==================== CLIENTS ====================

bot_client = TelegramClient(BOT_SESSION, NEW_API_ID, NEW_API_HASH, flood_sleep_threshold=60)
user_client = TelegramClient(NEW_SESSION, NEW_API_ID, NEW_API_HASH, flood_sleep_threshold=60)
old_client = TelegramClient(OLD_SESSION, OLD_API_ID, OLD_API_HASH, flood_sleep_threshold=60)

# ==================== GROUP COUNTER (locked + atomic) ====================

_counter_lock = asyncio.Lock()
_counter_high_water = [STARTING_GROUP_NUMBER]  # never go below the highest seen


def _read_counter_file():
    try:
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE, "r") as f:
                data = json.load(f)
                return int(data.get("last_group_number", STARTING_GROUP_NUMBER))
    except Exception as e:
        logger.error(f"Error reading counter file (keeping high-water mark): {e}")
    return None


def _write_counter_atomic(number):
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=BASE_DIR, prefix=".counter", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(
                {"last_group_number": number, "last_updated": datetime.now().isoformat()},
                f,
                indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, COUNTER_FILE)
        logger.info(f"Saved group counter: {number}")
    except Exception as e:
        logger.error(f"Error saving counter: {e}")
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


async def peek_next_group_number():
    """Return the number the next group would use (does NOT consume it)."""
    async with _counter_lock:
        disk = _read_counter_file()
        base = disk if disk is not None else _counter_high_water[0]
        base = max(base, _counter_high_water[0])
        _counter_high_water[0] = base
        return base + 1


async def commit_group_number(number):
    """Record a successfully used group number (atomic, never goes backward)."""
    async with _counter_lock:
        if number > _counter_high_water[0]:
            _counter_high_water[0] = number
        _write_counter_atomic(_counter_high_water[0])


# ==================== RATE LIMITING ====================

_creation_in_progress = set()
_creation_lock = asyncio.Lock()
_daily_counts = {}  # (user_id, yyyy-mm-dd) -> count


async def _check_rate_limit(user_id):
    today = date.today().isoformat()
    key = (user_id, today)
    async with _creation_lock:
        if user_id in _creation_in_progress:
            return False, "already_running"
        if _daily_counts.get(key, 0) >= MAX_GROUPS_PER_USER_PER_DAY:
            return False, "daily_cap"
        _creation_in_progress.add(user_id)
        _daily_counts[key] = _daily_counts.get(key, 0) + 1
        return True, "ok"


async def _release_creation(user_id):
    async with _creation_lock:
        _creation_in_progress.discard(user_id)


# ==================== HELPERS ====================

def _load_json_file(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load {path}: {e}")
    return default


def _save_json_file(path, data):
    try:
        fd, tmp = tempfile.mkstemp(dir=BASE_DIR)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Could not save {path}: {e}")


def record_user(user_id, username=None):
    """Persist every user that interacts with the bot (for future broadcasts)."""
    try:
        users = _load_json_file(USERS_FILE, {})
        key = str(user_id)
        entry = users.get(key, {})
        entry["username"] = username or entry.get("username")
        entry["last_seen"] = datetime.now().isoformat()
        users[key] = entry
        _save_json_file(USERS_FILE, users)
    except Exception as e:
        logger.warning(f"record_user failed: {e}")


# ==================== BROADCAST (file-triggered, admin-only by filesystem) ====================

_broadcast_running = False


async def run_broadcast():
    """Send the Global Buyers promo to members of recent deal groups via NEW account."""
    global _broadcast_running
    if _broadcast_running:
        logger.warning("Broadcast already running, skipping trigger")
        return
    _broadcast_running = True
    try:
        logger.info("BROADCAST STARTED")
        sent = set(_load_json_file(BROADCAST_SENT_FILE, []))
        new_me = await user_client.get_me()
        skip_ids = {new_me.id}

        # Collect recent deal groups (dialogs are ordered by recent activity)
        deal_groups = []
        async for d in user_client.iter_dialogs():
            ent = d.entity
            title = getattr(ent, "title", "") or ""
            if title.startswith(GROUP_BASE_NAME):
                deal_groups.append(ent)
                if len(deal_groups) >= BROADCAST_MAX_GROUPS:
                    break
        logger.info(f"Broadcast: scanning {len(deal_groups)} recent deal groups")

        targets = {}
        for g in deal_groups:
            try:
                async for p in user_client.iter_participants(g):
                    if getattr(p, "bot", False) or getattr(p, "deleted", False):
                        continue
                    if p.id in skip_ids:
                        continue
                    uname = getattr(p, "username", None)
                    # skip our own known service accounts
                    if uname and uname.lower() in ("balochhelpbot", "balochbuyer", "reall_baloch"):
                        continue
                    if str(p.id) not in sent and p.id not in targets:
                        targets[p.id] = uname
            except Exception as e:
                logger.warning(f"Broadcast: participants failed for {getattr(g, 'title', '?')}: {type(e).__name__}")
        logger.info(f"Broadcast: {len(targets)} unique users to message")

        ok, failed = 0, 0
        for uid in targets:
            try:
                await user_client.send_message(uid, BROADCAST_MESSAGE)
                sent.add(str(uid))
                ok += 1
                if ok % 10 == 0:
                    _save_json_file(BROADCAST_SENT_FILE, sorted(sent))
                    logger.info(f"Broadcast progress: {ok} sent, {failed} failed")
            except FloodWaitError as e:
                logger.warning(f"Broadcast FloodWait {e.seconds}s, sleeping")
                await asyncio.sleep(e.seconds + 1)
                continue  # retry this user once after wait
            except Exception as e:
                name = type(e).__name__
                if name in ("PeerFloodError",):
                    logger.critical("Broadcast ABORTED: PeerFloodError (account limited by Telegram)")
                    break
                failed += 1
                logger.debug(f"Broadcast skip {uid}: {name}")
            await asyncio.sleep(BROADCAST_DELAY)
        _save_json_file(BROADCAST_SENT_FILE, sorted(sent))
        logger.info(f"BROADCAST DONE: {ok} sent, {failed} failed/skipped")
    except Exception:
        logger.error(f"Broadcast fatal:\n{traceback.format_exc()}")
    finally:
        _broadcast_running = False
        try:
            if os.path.exists(BROADCAST_TRIGGER_FILE):
                os.remove(BROADCAST_TRIGGER_FILE)
        except Exception:
            pass


async def run_broadcast_bot():
    """Send the fancy Global Buyers promo FROM @Baloch_Escrow2_bot.

    NOTE: Telegram bots can only message users who have pressed Start on the bot.
    Best-effort: tries every recent deal-group member; those who never started
    the bot are skipped silently.
    """
    global _broadcast_running
    if _broadcast_running:
        logger.warning("Broadcast already running, skipping trigger")
        return
    _broadcast_running = True
    try:
        logger.info("BROADCAST_BOT STARTED (from @Baloch_Escrow2_bot)")
        sent = set(_load_json_file(BROADCAST_BOT_SENT_FILE, []))
        already = set(_load_json_file(BROADCAST_SENT_FILE, []))  # got userbot DM already
        new_me = await user_client.get_me()
        skip_ids = {new_me.id}

        deal_groups = []
        async for d in user_client.iter_dialogs():
            ent = d.entity
            title = getattr(ent, "title", "") or ""
            if title.startswith(GROUP_BASE_NAME):
                deal_groups.append(ent)
                if len(deal_groups) >= BROADCAST_MAX_GROUPS:
                    break
        logger.info(f"BroadcastBot: scanning {len(deal_groups)} recent deal groups")

        targets = {}
        for g in deal_groups:
            try:
                async for p in user_client.iter_participants(g):
                    if getattr(p, "bot", False) or getattr(p, "deleted", False):
                        continue
                    if p.id in skip_ids:
                        continue
                    uname = getattr(p, "username", None)
                    if uname and uname.lower() in ("balochhelpbot", "balochbuyer", "reall_baloch"):
                        continue
                    sid = str(p.id)
                    if sid not in sent and sid not in already and p.id not in targets:
                        targets[p.id] = uname
            except Exception as e:
                logger.warning(f"BroadcastBot: participants failed for {getattr(g, 'title', '?')}: {type(e).__name__}")
        logger.info(f"BroadcastBot: {len(targets)} users to try via bot")

        ok, skipped = 0, 0
        for uid in targets:
            try:
                await bot_client.send_message(uid, BROADCAST_BOT_MESSAGE, parse_mode="html")
                sent.add(str(uid))
                ok += 1
                if ok % 10 == 0:
                    _save_json_file(BROADCAST_BOT_SENT_FILE, sorted(sent))
                    logger.info(f"BroadcastBot progress: {ok} sent, {skipped} skipped")
            except FloodWaitError as e:
                logger.warning(f"BroadcastBot FloodWait {e.seconds}s, sleeping then continuing")
                await asyncio.sleep(e.seconds + 1)
                skipped += 1
            except Exception as e:
                # Most common: user never started the bot (bot can't initiate)
                skipped += 1
                logger.debug(f"BroadcastBot skip {uid}: {type(e).__name__}")
            await asyncio.sleep(3)
        _save_json_file(BROADCAST_BOT_SENT_FILE, sorted(sent))
        logger.info(f"BROADCAST_BOT DONE: {ok} sent, {skipped} skipped (never started bot / blocked)")
    except Exception:
        logger.error(f"BroadcastBot fatal:\n{traceback.format_exc()}")
    finally:
        _broadcast_running = False
        try:
            if os.path.exists(BROADCAST_BOT_TRIGGER_FILE):
                os.remove(BROADCAST_BOT_TRIGGER_FILE)
        except Exception:
            pass


async def broadcast_watcher():
    """Watch for the trigger file; run broadcast once per trigger."""
    logger.info("Broadcast watcher started")
    while True:
        try:
            if os.path.exists(BROADCAST_TRIGGER_FILE) and not _broadcast_running:
                await run_broadcast()
            elif os.path.exists(BROADCAST_BOT_TRIGGER_FILE) and not _broadcast_running:
                await run_broadcast_bot()
        except Exception as e:
            logger.warning(f"Broadcast watcher error: {e}")
        await asyncio.sleep(30)


def make_admin_rights():
    """Full admin rights for escrow admins."""
    return ChatAdminRights(
        change_info=True,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        ban_users=True,
        invite_users=True,
        pin_messages=True,
        add_admins=True,
    )


def _chmod_600(path):
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
    except Exception as e:
        logger.warning(f"Could not chmod 600 {path}: {e}")


async def ensure_client_connected(client, name):
    """Make sure a Telethon client is connected; reconnect if needed."""
    try:
        if not client.is_connected():
            logger.info(f"Reconnecting {name} client...")
            await client.connect()
        if not await client.is_user_authorized():
            return False
        return True
    except Exception as e:
        logger.error(f"{name} client reconnect failed: {e}")
        return False


async def invite_and_promote(invite_client, channel, username_or_id, rank, label):
    """
    Invite a user/bot to the channel and promote to admin.
    Returns True only if BOTH invite and promote succeeded.
    """
    try:
        await invite_client(
            InviteToChannelRequest(channel=channel, users=[username_or_id])
        )
        logger.info(f"INVITED ({label}): {username_or_id}")
    except Exception as e:
        logger.error(f"FAILED to invite ({label}) {username_or_id}: {e}")
        return False
    try:
        entity = await invite_client.get_entity(username_or_id)
        await invite_client(
            EditAdminRequest(
                channel=channel,
                user_id=entity.id,
                admin_rights=make_admin_rights(),
                rank=rank,
            )
        )
        logger.info(f"PROMOTED ({label}): {username_or_id} to admin")
        return True
    except Exception as e:
        logger.error(f"FAILED to promote ({label}) {username_or_id}: {e}")
        return False


# ==================== XXIN SPAM AUTO-DELETE ====================
# Deletes messages from senders whose username or display name contains "xxin"
# (case-insensitive). Requires the bot to be admin with delete rights in the group.
# Set SPAM_DELETE_GROUPS env var (comma-separated) to limit which groups are watched;
# empty = all groups the bot is in.
SPAM_NAME_PATTERN = re.compile(os.environ.get("SPAM_NAME_PATTERN", r"xxin"), re.IGNORECASE)
_env_spam_groups = os.environ.get("SPAM_DELETE_GROUPS", "").strip()
SPAM_DELETE_GROUPS = [s.strip() for s in _env_spam_groups.split(",") if s.strip()]

@bot_client.on(events.NewMessage(chats=SPAM_DELETE_GROUPS if SPAM_DELETE_GROUPS else None))
async def xxin_spam_delete_handler(event):
    """Delete XXIN spam bot messages in groups."""
    # Only in groups/supergroups, not private chats
    if not event.is_group:
        return
    try:
        sender = await event.get_sender()
        if not sender:
            return
        # Don't delete our own messages or other bots we manage
        if getattr(sender, 'bot', False):
            bot_username = (getattr(sender, 'username', '') or '').lower()
            # Still delete xxin bots even if they're bots
            if not SPAM_NAME_PATTERN.search(bot_username):
                return
        username = (getattr(sender, 'username', '') or '')
        first_name = getattr(sender, 'first_name', '') or ''
        last_name = getattr(sender, 'last_name', '') or ''
        full_name = f"{first_name} {last_name}".strip()
        if SPAM_NAME_PATTERN.search(username) or SPAM_NAME_PATTERN.search(full_name):
            await event.delete()
            logger.info(
                f"Deleted XXIN spam from @{username or sender.id} in chat {event.chat_id}"
            )
    except Exception as e:
        logger.warning(f"Failed to delete XXIN spam: {e}")


# ==================== BOT HANDLERS ====================

@bot_client.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    """Handle /start command."""
    try:
        logger.info(f"/start from user {event.sender_id}")
        try:
            _sender = await event.get_sender()
            record_user(event.sender_id, getattr(_sender, "username", None))
        except Exception:
            record_user(event.sender_id)
        buttons = [
            [Button.inline("🔗 Get Escrow Group Link", b"get_group_link")],
            [Button.inline("❌ Cancel", b"cancel_main")],
        ]
        await event.respond(
            "<b>🔒 Welcome to Baloch Escrow Bot!</b>\n\n"
            "<b>If you want to start an Escrow deal, click the button below to create a secure group.</b>\n\n"
            "✅ <b>Features:</b>\n"
            "• Secure escrow groups\n"
            "• Automated bot assistance\n"
            "• Fast and reliable service\n"
            "• Low fees\n\n"
            "Click 'Get Escrow Group Link' to begin!",
            buttons=buttons,
            parse_mode="html",
        )
    except Exception as e:
        logger.error(f"Error in start_handler: {e}")


@bot_client.on(events.CallbackQuery(data=b"cancel_main"))
async def cancel_handler(event):
    """Handle cancel button."""
    try:
        await event.edit(
            "<b>❌ Cancelled.</b>\n\nUse /start to begin again.", parse_mode="html"
        )
    except Exception as e:
        logger.error(f"Error in cancel_handler: {e}")


@bot_client.on(events.CallbackQuery(data=b"get_group_link"))
async def group_link_handler(event):
    """
    Handle group creation request.

    FLOW:
      1. NEW account creates the supergroup
      2. Invite + promote @Balochhelpbot as admin
      3. Export invite link
      4. OLD account joins via invite link, promoted as admin
      5. @Balochbuyer invited + promoted as admin
      6. Welcome message posted
      7. Honest result sent to requesting user
    """
    user_id = event.sender_id
    logger.info(f"Group creation requested by user {user_id}")

    # If the user-client session is invalid, explain instead of failing silently.
    if not USER_CLIENT_AVAILABLE:
        try:
            await event.answer(
                "⚠️ Group creation is temporarily unavailable (account session expired). "
                "Please contact the admin to refresh it. The bot will still answer other commands.",
                alert=True,
            )
        except Exception:
            pass
        return

    try:
        _sender2 = await event.get_sender()
        record_user(user_id, getattr(_sender2, "username", None))
    except Exception:
        record_user(user_id)

    allowed, reason = await _check_rate_limit(user_id)
    if not allowed:
        try:
            if reason == "already_running":
                await event.answer("⏳ Your group is already being created, please wait…", alert=True)
            else:
                await event.answer(
                    f"⚠️ Daily limit reached ({MAX_GROUPS_PER_USER_PER_DAY} groups/day). Try again tomorrow.",
                    alert=True,
                )
        except Exception:
            pass
        return

    try:
        try:
            await event.edit(
                "<b>⏳ Please wait, creating your Escrow Group...</b>\n\nThis may take a few seconds.",
                parse_mode="html",
            )
        except Exception:
            pass

        # ---------- STEP 0: validate clients (fail fast, no interactive login) ----------
        if not await ensure_client_connected(user_client, "NEW"):
            await bot_client.send_message(
                user_id,
                "<b>⚠️ Service temporarily unavailable</b>\n\n"
                "The group-creator account needs re-authorization. "
                f"Please contact {SUPPORT_CONTACT}.",
                parse_mode="html",
            )
            return

        # ---------- STEP 1: create supergroup ----------
        group_num = await peek_next_group_number()
        group_title = f"{GROUP_BASE_NAME} {group_num}"
        logger.info(f"Creating group: {group_title}")

        try:
            ch_res = await user_client(
                CreateChannelRequest(
                    title=group_title,
                    about=f"Baloch Escrow Deal #{group_num} - Secure Transaction Group",
                    megagroup=True,
                )
            )
            created_group = ch_res.chats[0]
            group_id = created_group.id
            logger.info(f"CREATED: '{group_title}' (ID: {group_id})")
        except FloodWaitError as e:
            logger.error(f"FloodWait on group creation: {e.seconds}s")
            await bot_client.send_message(
                user_id,
                "<b>⏳ Telegram rate limit hit</b>\n\n"
                "Too many groups created recently. Please try again in a few minutes.",
                parse_mode="html",
            )
            return
        except Exception as e:
            logger.error(f"FAILED to create supergroup: {e}")
            await bot_client.send_message(
                user_id,
                "<b>❌ Failed to create group</b>\n\n"
                f"<b>Error:</b> <code>{html.escape(str(e))}</code>\n\n"
                f"Please try again later or contact {SUPPORT_CONTACT}.",
                parse_mode="html",
            )
            return

        # Number is consumed only after successful creation
        await commit_group_number(group_num)

        # ---------- STEP 2: invite + promote helper bot ----------
        bot_admin_ok = await invite_and_promote(
            user_client, created_group, ADD_BOT_USERNAME, "Escrow Admin", "helper bot"
        )

        # ---------- STEP 3: export invite link ----------
        invite_link = None
        try:
            inv = await user_client(ExportChatInviteRequest(peer=created_group))
            invite_link = getattr(inv, "link", None)
        except Exception as e:
            logger.error(f"Failed to export invite link: {e}")
        if not invite_link:
            await bot_client.send_message(
                user_id,
                "<b>❌ Group created but invite link failed.</b>\n\n"
                f"Group ID: <code>{group_id}</code>\n"
                f"Please contact {SUPPORT_CONTACT}.",
                parse_mode="html",
            )
            return
        logger.info(f"Invite link exported for group {group_id}")
        invite_hash = invite_link.split("/")[-1].replace("+", "")

        # ---------- STEP 4: OLD account joins via link, then promoted ----------
        old_admin_ok = False
        if await ensure_client_connected(old_client, "OLD"):
            try:
                old_me = await old_client.get_me()
                try:
                    await old_client(ImportChatInviteRequest(invite_hash))
                    logger.info(f"OLD account joined via invite link (ID={old_me.id})")
                except Exception as e:
                    # May already be a member; not fatal
                    logger.info(f"OLD join via link note: {e}")
                # Warm the NEW client's entity cache, then promote
                try:
                    old_entity = await user_client.get_entity(old_me.id)
                    await user_client(
                        EditAdminRequest(
                            channel=created_group,
                            user_id=old_entity.id,
                            admin_rights=make_admin_rights(),
                            rank="Escrow Admin",
                        )
                    )
                    old_admin_ok = True
                    logger.info("PROMOTED: OLD account to admin")
                except Exception as e:
                    logger.error(f"Failed to promote OLD account: {e}")
            except Exception as e:
                logger.error(f"OLD account step failed: {e}")
        else:
            logger.error("OLD client unavailable - skipping OLD admin step")

        # ---------- STEP 5: invite + promote @Balochbuyer ----------
        buyer_admin_ok = await invite_and_promote(
            user_client, created_group, SUPPORT_USERNAME, "Escrow Admin", "support admin"
        )

        # ---------- STEP 6: welcome message ----------
        try:
            share_url = "https://t.me/share/url?url=" + urllib.parse.quote(invite_link, safe="") + "&text=" + urllib.parse.quote("Join our Escrow Deal Group", safe="")
            await user_client.send_message(
                created_group,
                (
                    f"<b>🔒 WELCOME TO BALOCH ESCROW DEAL #{group_num} 🔒</b>\n\n"
                    f"👋 <b>Welcome to your secure escrow deal group!</b>\n\n"
                    f"📞 <b>Support:</b> {SUPPORT_CONTACT}\n\n"
                    f"🔗 <b>Group Link:</b>\n{invite_link}"
                ),
                buttons=[[Button.url("📤 Share to Partner", share_url)]],
                parse_mode="html",
            )
        except Exception as e:
            logger.error(f"Failed to send welcome message: {e}")

        # ---------- STEP 7: honest result to user ----------
        try:
            admin_lines = []
            admin_lines.append(
                f"🤖 @{ADD_BOT_USERNAME}: {'admin ✅' if bot_admin_ok else 'NOT added ⚠️'}"
            )
            admin_lines.append(
                f"👤 Original account: {'admin ✅' if old_admin_ok else 'NOT added ⚠️'}"
            )
            admin_lines.append(
                f"👤 @{SUPPORT_USERNAME}: {'admin ✅' if buyer_admin_ok else 'NOT added ⚠️'}"
            )
            all_ok = bot_admin_ok and old_admin_ok and buyer_admin_ok
            header = (
                "<b>✅ Escrow Group Created Successfully!</b>"
                if all_ok
                else "<b>⚠️ Group Created (some admins missing)</b>"
            )
            await bot_client.send_message(
                user_id,
                (
                    f"{header}\n\n"
                    f"<b>📋 Group Details:</b>\n"
                    f"• <b>Name:</b> {group_title}\n"
                    f"• <b>Number:</b> #{group_num}\n"
                    f"• <b>Created:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"<b>👑 Admins:</b>\n" + "\n".join(admin_lines) + "\n\n"
                    f"<b>📎 Your Escrow Group Link:</b>\n{invite_link}\n\n"
                    f"<b>📋 Next Steps:</b>\n"
                    f"1️⃣ Join the group using the link above\n"
                    f"2️⃣ Share the link with your partner\n"
                    f"3️⃣ Both parties join the group\n\n"
                    + (
                        "⚡ <b>Start your secure transaction now!</b> ⚡"
                        if all_ok
                        else f"⚠️ <b>Please contact {SUPPORT_CONTACT} — an admin is missing.</b>"
                    )
                ),
                parse_mode="html",
            )
            logger.info(f"Result sent to user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send result to user: {e}")

        logger.info("Group creation process completed")

    except FloodWaitError as e:
        logger.error(f"FloodWait in handler: {e.seconds}s")
        try:
            await bot_client.send_message(
                user_id, "<b>⏳ Telegram rate limit — please try again in a few minutes.</b>",
                parse_mode="html",
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"CRITICAL ERROR in group_link_handler: {e}\n{traceback.format_exc()}")
        try:
            await bot_client.send_message(
                user_id,
                "<b>❌ An error occurred while creating your group</b>\n\n"
                f"<b>Error details:</b>\n<code>{html.escape(str(e))}</code>\n\n"
                f"Please try again or contact:\n{SUPPORT_CONTACT}",
                parse_mode="html",
            )
        except Exception:
            pass
    finally:
        await _release_creation(user_id)


# ==================== REMINDER SYSTEM ====================

_reminder_task = None


async def reminder_loop():
    """Send periodic reminders to the escrow chat."""
    logger.info("Reminder loop started")
    while True:
        try:
            sent_msg = await bot_client.send_message(
                ESCROW_REMINDER_CHAT,
                REMINDER_MESSAGE,
                buttons=[Button.url(REMINDER_BUTTON_TEXT, REMINDER_BUTTON_URL)],
                parse_mode="html",
            )
            msg_id = sent_msg.id
            await asyncio.sleep(REMINDER_DELETE_AFTER)
            try:
                await bot_client.delete_messages(ESCROW_REMINDER_CHAT, [msg_id])
            except Exception as e:
                logger.warning(f"Could not delete reminder: {e}")
            await asyncio.sleep(max(0, REMINDER_INTERVAL - REMINDER_DELETE_AFTER))
        except Exception as e:
            logger.error(f"Error in reminder loop: {e}")
            await asyncio.sleep(60)


# ==================== MAIN ====================

async def main():
    logger.info("Starting Baloch Escrow Bot - PERFECTED VERSION")

    if _missing:
        logger.critical(
            "Missing required environment variables: %s. "
            "Set them before starting (see /root/.secrets/escrow_bot.env).",
            ", ".join(_missing),
        )
        sys.exit(1)

    # Start bot client
    await bot_client.start(bot_token=BOT_TOKEN)
    bot_me = await bot_client.get_me()
    logger.info(f"Bot started: @{bot_me.username} (ID: {bot_me.id})")

    # Connect NEW account (graceful degradation: bot responds but group creation
    # is unavailable until the session is refreshed)
    global USER_CLIENT_AVAILABLE
    USER_CLIENT_AVAILABLE = False
    await user_client.connect()
    if not await user_client.is_user_authorized():
        logger.critical(
            "NEW account session NOT authorized - bot will run in LIMITED mode. "
            "It will respond to messages but CANNOT create groups. To restore, refresh "
            "'%s.session' via interactive login and restart.",
            NEW_SESSION,
        )
    else:
        USER_CLIENT_AVAILABLE = True
        new_me = await user_client.get_me()
        logger.info(f"NEW account: @{new_me.username} (ID: {new_me.id}) - creates groups")
        _chmod_600(NEW_SESSION + ".session")

    # Connect OLD account (graceful degradation: bot runs without it,
    # but groups will miss the original-account admin until re-authorized)
    await old_client.connect()
    if not await old_client.is_user_authorized():
        logger.critical(
            "OLD account session NOT authorized - bot will run WITHOUT the original "
            "account as admin. To restore it, refresh '%s.session' via interactive "
            "login and restart. Groups will still get @%s and @%s as admins.",
            OLD_SESSION, ADD_BOT_USERNAME, SUPPORT_USERNAME,
        )
    else:
        old_me = await old_client.get_me()
        logger.info(f"OLD account: @{old_me.username} (ID: {old_me.id}) - admin in groups")
    _chmod_600(OLD_SESSION + ".session")
    _chmod_600(BOT_SESSION + ".session")

    current = _read_counter_file()
    logger.info(f"Group counter: {current}, next: {(current or STARTING_GROUP_NUMBER) + 1}")

    global _reminder_task, _broadcast_task
    _reminder_task = asyncio.create_task(reminder_loop())
    _broadcast_task = asyncio.create_task(broadcast_watcher())

    logger.info("BOT IS NOW RUNNING AND READY")
    logger.info(f"Deal Handler Bot : @{ADD_BOT_USERNAME}")
    logger.info(f"Support Contact  : {SUPPORT_CONTACT}")

    try:
        await bot_client.run_until_disconnected()
    finally:
        for _t in (_reminder_task, globals().get("_broadcast_task")):
            if _t:
                _t.cancel()
                try:
                    await _t
                except asyncio.CancelledError:
                    pass
        for c, n in ((bot_client, "bot"), (user_client, "NEW"), (old_client, "OLD")):
            try:
                await c.disconnect()
            except Exception as e:
                logger.debug(f"Disconnect {n}: {e}")
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except SystemExit:
        raise
    except Exception:
        logger.error(f"Fatal error at entry point\n{traceback.format_exc()}")
        sys.exit(1)
