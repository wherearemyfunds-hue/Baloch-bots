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
import re
import sys
import tempfile
import time
import traceback
import urllib.parse
from datetime import datetime, date
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    EditAdminRequest,
    EditTitleRequest,
    EditBannedRequest,
)
try:
    from telethon.tl.functions.channels import EditAboutRequest
    _HAS_EDIT_ABOUT = True
except ImportError:
    EditAboutRequest = None
    _HAS_EDIT_ABOUT = False
from telethon.tl.functions.messages import (
    ExportChatInviteRequest,
    ImportChatInviteRequest,
    CheckChatInviteRequest,
    EditExportedChatInviteRequest,
    GetExportedChatInvitesRequest,
)
from telethon.tl.types import (
    ChatAdminRights, ChatBannedRights, ChannelParticipantsAdmins,
    ChannelParticipantsKicked,
)

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
ANIMATION_FILE = os.path.join(BASE_DIR, "escrow_animation.mp4")

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


# ==================== GROUP RECYCLE POOL ====================
# Shawn (2026-10-12): reuse dead deal groups instead of creating new ones.
# A group becomes spare when: age >= 2 days, NO payment ever confirmed,
# and no messages for 2 days. Reuse renames it, wipes history, kicks
# non-service members, revokes invite links, and bumps addr_gen so the
# new deal gets FRESH deposit addresses (no confusion with late payments
# to the old address). Deal numbers keep incrementing normally.
POOL_FILE = os.path.join(BASE_DIR, "group_pool.json")
RECYCLE_AFTER_DAYS = 2
RECYCLE_IDLE_DAYS = 2
DEAL_BOT_USERNAME = "Baloch_Escrow2_bot"


def _load_pool() -> dict:
    try:
        if os.path.exists(POOL_FILE):
            with open(POOL_FILE, "r") as f:
                d = json.load(f)
                d.setdefault("groups", {})
                d.setdefault("needs_reset", [])
                d.setdefault("pending_cleanup", [])
                d.setdefault("link_requests", [])
                return d
    except Exception as e:
        logger.error(f"pool load failed: {e}")
    return {"groups": {}, "needs_reset": [], "pending_cleanup": [], "link_requests": []}


def _save_pool(pool: dict):
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=BASE_DIR, prefix=".pool", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(pool, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, POOL_FILE)
    except Exception as e:
        logger.error(f"pool save failed: {e}")
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _full_chat_id(bare_id: int) -> int:
    """Telethon channel id -> full -100... chat id (matches deal bot's chat_id)."""
    return int(f"-100{bare_id}")


def _register_group(chat_id: int, deal_no: int, title: str):
    pool = _load_pool()
    g = pool["groups"].setdefault(str(chat_id), {})
    g.update({
        "created_at": time.time(),
        "status": "active",
        "deal_no": deal_no,
        "title": title,
        "addr_gen": g.get("addr_gen", 0),
    })
    _save_pool(pool)


def _get_spare_group():
    """Oldest spare group's full chat id, or None."""
    pool = _load_pool()
    spares = [
        (g.get("created_at", 0), cid)
        for cid, g in pool["groups"].items()
        if g.get("status") == "spare"
    ]
    if not spares:
        return None
    spares.sort()
    return int(spares[0][1])


def _mark_active(chat_id: int, deal_no: int, title: str):
    """Mark a reused spare as active; bump addr_gen for fresh deposit addresses."""
    pool = _load_pool()
    g = pool["groups"].setdefault(str(chat_id), {})
    g.update({
        "created_at": g.get("created_at", time.time()),
        "status": "active",
        "deal_no": deal_no,
        "title": title,
        "addr_gen": g.get("addr_gen", 0) + 1,
    })
    # RACE FIX: a reused group must not be nuked by a stale pending_cleanup
    # flag - the cleanup loop would revoke the NEW deal's fresh invite link,
    # which is exactly the "this invite link has expired" bug.
    pc = pool.get("pending_cleanup", [])
    key = str(chat_id)
    if key in pc:
        pc.remove(key)
        logger.info(f"mark_active: dropped stale pending_cleanup for {chat_id} (reused)")
    _save_pool(pool)


def _needs_reset_add(chat_id: int):
    """Signal the deal bot to reset its in-memory state for this group on next join."""
    pool = _load_pool()
    key = str(chat_id)
    if key not in pool["needs_reset"]:
        pool["needs_reset"].append(key)
    _save_pool(pool)


async def _service_ids() -> set:
    """User/bot ids that must NEVER be kicked from a deal group."""
    ids = set()
    try:
        me = await user_client.get_me()
        ids.add(me.id)
    except Exception:
        pass
    try:
        old_me = await old_client.get_me()
        ids.add(old_me.id)
    except Exception:
        pass
    for uname in (ADD_BOT_USERNAME, SUPPORT_USERNAME, DEAL_BOT_USERNAME):
        try:
            ent = await user_client.get_entity(uname)
            ids.add(ent.id)
        except Exception:
            pass
    return ids


async def _clear_group_history(channel):
    try:
        batch = []
        async for m in user_client.iter_messages(channel, limit=10000):
            batch.append(m.id)
            if len(batch) >= 100:
                try:
                    await user_client.delete_messages(channel, batch)
                except Exception:
                    pass
                batch = []
        if batch:
            try:
                await user_client.delete_messages(channel, batch)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"clear history failed: {e}")


async def _kick_non_service(channel):
    try:
        keep = await _service_ids()
        async for u in user_client.iter_participants(channel):
            if u.id in keep or getattr(u, "bot", False):
                continue
            try:
                await user_client(EditBannedRequest(
                    channel, u.id,
                    ChatBannedRights(until_date=None, view_messages=True)))
                logger.info(f"kicked {u.id} from recycled group")
            except Exception as e:
                logger.warning(f"kick {u.id} failed: {e}")
    except Exception as e:
        logger.warning(f"kick sweep failed: {e}")


async def _unban_all(channel):
    """Lift every ban in the group.

    Recycle kicks use permanent bans, so a reused group accumulates
    banned users from its past lives - and a banned user can never open
    a fresh invite link for this group (Telegram shows it as expired).
    A reused group must start with a clean ban list. Safe right after
    the kick sweep: kicked users still cannot return, because every
    old invite link is revoked and the new one is minted afterwards.
    """
    try:
        n = 0
        async for u in user_client.iter_participants(
                channel, filter=ChannelParticipantsKicked):
            try:
                await user_client(EditBannedRequest(
                    channel, u.id, ChatBannedRights(until_date=None)))
                n += 1
            except Exception:
                pass
        if n:
            logger.info(f"unbanned {n} past-life users from recycled group")
    except Exception as e:
        logger.warning(f"unban sweep failed: {e}")


async def _revoke_invite_links(channel, keep_link: str = None):
    """Revoke all invite links EXCEPT keep_link (the active deal's link).

    The active deal's link is never revoked, so users never see
    "this link expired" while the deal is running.
    Returns the set of link strings that were revoked.
    """
    revoked = set()
    try:
        cur = await user_client(ExportChatInviteRequest(peer=channel))
        link = getattr(cur, "link", None)
        if link and link != keep_link:
            try:
                await user_client(EditExportedChatInviteRequest(
                    peer=channel, link=link, revoked=True))
                revoked.add(link)
            except Exception as e:
                logger.warning(f"revoke primary link failed: {e}")
    except Exception as e:
        logger.warning(f"export current link failed: {e}")
    try:
        me = await user_client.get_me()
        invs = await user_client(GetExportedChatInvitesRequest(
            peer=channel, admin_id=me.id, limit=50))
        for iv in getattr(invs, "invites", []) or []:
            try:
                if getattr(iv, "link", None) == keep_link:
                    continue  # never revoke the active deal's link
                await user_client(EditExportedChatInviteRequest(
                    peer=channel, link=iv.link, revoked=True))
                revoked.add(iv.link)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"revoke extra links failed: {e}")
    return revoked


async def _prepare_spare_group(entity, new_title: str):
    """Rename + wipe + revoke a spare group for reuse.

    Returns the set of invite links revoked during prep on success,
    or None on failure.
    """
    try:
        await user_client(EditTitleRequest(channel=entity, title=new_title))
    except Exception as e:
        logger.error(f"spare rename failed: {e}")
        return None
    # Also refresh the about/description - otherwise the preview shows the
    # OLD deal number (e.g. title "Deal 3135" but about "Deal #3118"), which
    # looks like a wrong/expired group to users.
    # (Skipped on old telethon without EditAboutRequest.)
    try:
        if not _HAS_EDIT_ABOUT:
            raise RuntimeError("EditAboutRequest not available in this telethon")
        _m = re.search(r"(\d+)\s*$", new_title)
        _num = _m.group(1) if _m else ""
        if _num:
            await user_client(EditAboutRequest(
                channel=entity,
                about=f"{GROUP_BASE_NAME} #{_num} - Secure Transaction Group",
            ))
            logger.info(f"spare about updated to deal #{_num}")
    except Exception as e:
        logger.warning(f"spare about update failed: {e}")
    await _clear_group_history(entity)
    await _kick_non_service(entity)
    await _unban_all(entity)
    # Revoke ALL old invite links - a reused spare always gets a freshly
    # exported link afterwards, so nobody ever taps an "expired" link
    # from a previous deal.
    revoked_links = await _revoke_invite_links(entity)
    try:
        _pool = _load_pool()
        _g = _pool.get("groups", {}).get(str(_full_chat_id(entity.id)), {})
        if isinstance(_g, dict) and "active_link" in _g:
            _g.pop("active_link", None)
            _save_pool(_pool)
    except Exception:
        pass
    return revoked_links


def _load_deal_states_file() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "deal_states.json"), "r") as f:
            return json.load(f)
    except Exception:
        return {}


async def _kick_non_admins(channel):
    """Kick everyone except service accounts, bots, and channel admins.

    Used by the admin-triggered bulk cleanup: human admins stay, everyone
    else goes, so the group is empty but still manageable.
    """
    try:
        keep = await _service_ids()
        try:
            async for u in user_client.iter_participants(channel, filter=ChannelParticipantsAdmins):
                keep.add(u.id)
        except Exception as e:
            logger.warning(f"admin list fetch failed: {e}")
        n = 0
        async for u in user_client.iter_participants(channel):
            if u.id in keep or getattr(u, "bot", False):
                continue
            try:
                await user_client(EditBannedRequest(
                    channel, u.id,
                    ChatBannedRights(until_date=None, view_messages=True)))
                n += 1
            except Exception as e:
                logger.warning(f"kick {u.id} failed: {e}")
        logger.info(f"kicked {n} non-admin users from recycled group")
    except Exception as e:
        logger.warning(f"kick non-admins failed: {e}")


async def _recycle_group_to_spare(chat_id: int, title: str, keep_admins: bool = False) -> bool:
    try:
        entity = await user_client.get_entity(chat_id)
    except Exception as e:
        logger.error(f"recycle: get_entity {chat_id} failed: {e}")
        return False
    await _clear_group_history(entity)
    if keep_admins:
        await _kick_non_admins(entity)
    else:
        await _kick_non_service(entity)
    # Deal is over — revoke all links and clear the stored active link.
    await _revoke_invite_links(entity)
    try:
        _pool = _load_pool()
        _g = _pool.get("groups", {}).get(str(chat_id), {})
        if isinstance(_g, dict) and "active_link" in _g:
            _g.pop("active_link", None)
            _save_pool(_pool)
    except Exception:
        pass
    try:
        spare_title = title if title.endswith("♻️") else f"{title} ♻️"
        await user_client(EditTitleRequest(channel=entity, title=spare_title))
    except Exception as e:
        logger.warning(f"recycle rename failed: {e}")
    return True


async def _recycle_scan():
    """Daily: discover deal groups, move dead ones (2d+, no payment, idle 2d) to spare."""
    if not USER_CLIENT_AVAILABLE:
        logger.warning("recycle scan skipped: user client unavailable")
        return
    if not await ensure_client_connected(user_client, "NEW"):
        logger.warning("recycle scan skipped: could not connect user client")
        return
    pool = _load_pool()
    # --- discover existing deal groups not yet tracked ---
    try:
        async for d in user_client.iter_dialogs():
            ent = d.entity
            if not getattr(ent, "megagroup", False):
                continue
            title = getattr(ent, "title", "") or ""
            if not title.startswith(GROUP_BASE_NAME):
                continue
            cid = _full_chat_id(ent.id)
            if str(cid) not in pool["groups"]:
                created = getattr(ent, "date", None)
                pool["groups"][str(cid)] = {
                    "created_at": created.timestamp() if created else time.time(),
                    "status": "spare" if title.endswith("♻️") else "active",
                    "deal_no": None,
                    "title": title,
                    "addr_gen": 0,
                }
                logger.info(f"recycle: discovered group {cid} '{title}'")
    except Exception as e:
        logger.error(f"recycle discovery failed: {e}")
    # --- evaluate active groups ---
    deal_states = _load_deal_states_file()
    now = time.time()
    for cid_str, g in list(pool["groups"].items()):
        if g.get("status") != "active":
            continue
        if now - g.get("created_at", now) < RECYCLE_AFTER_DAYS * 86400:
            continue
        cid = int(cid_str)
        st = deal_states.get(cid_str)
        if st and st.get("payment_confirmed_at"):
            continue  # payment happened — never recycle
        try:
            msgs = await user_client.get_messages(cid, limit=1)
        except Exception as e:
            logger.warning(f"recycle: history check {cid} failed: {e}")
            continue
        last_ts = msgs[0].date.timestamp() if msgs else g.get("created_at", 0)
        if now - last_ts < RECYCLE_IDLE_DAYS * 86400:
            continue
        logger.info(f"recycle: {cid} eligible -> spare")
        if await _recycle_group_to_spare(cid, g.get("title") or GROUP_BASE_NAME):
            g["status"] = "spare"
            logger.info(f"recycle: {cid} is now spare")
    _save_pool(pool)


async def recycle_loop():
    """Daily maintenance: recycle dead deal groups into the spare pool."""
    await asyncio.sleep(600)  # let startup settle
    while True:
        try:
            await _recycle_scan()
        except Exception as e:
            logger.error(f"recycle scan error: {e}")
        await asyncio.sleep(24 * 3600)


async def cleanup_loop():
    """Process unpaid-deal cleanup requests from the manager bot.

    The manager flags group chat ids in pool['pending_cleanup'] after the
    5h payment window + 12h reminder expire with no payment. We clear the
    chat, kick non-service members, revoke links, and mark spare for reuse.

    Also handles pool['link_requests']: export a fresh invite link for a
    deal group (when the old one expired) and DM it to the requesting admin.
    """
    await asyncio.sleep(300)  # let startup settle
    while True:
        try:
            pool = _load_pool()
            pending = pool.get("pending_cleanup", [])
            link_reqs = pool.get("link_requests", [])
            if not pending and not link_reqs:
                await asyncio.sleep(600)
                continue
            deal_states = _load_deal_states_file()
            for cid_str in list(pending):
                try:
                    cid = int(cid_str)
                except (ValueError, TypeError):
                    pending.remove(cid_str)
                    continue
                g = pool.get("groups", {}).get(cid_str)
                if isinstance(g, dict) and g.get("status") == "spare":
                    # Already recycled - drop the stale flag.
                    pending.remove(cid_str)
                    continue
                st = deal_states.get(cid_str)
                if st and st.get("payment_confirmed_at"):
                    # Paid after being flagged - never recycle; drop the flag.
                    pending.remove(cid_str)
                    logger.info(f"cleanup: {cid} paid after flagging, skipping")
                    continue
                title = pool.get("groups", {}).get(cid_str, {}).get(
                    "title", GROUP_BASE_NAME)
                logger.info(f"cleanup: processing unpaid deal group {cid}")
                ok = await _recycle_group_to_spare(cid, title)
                if ok:
                    g = pool.get("groups", {}).get(cid_str)
                    if isinstance(g, dict):
                        g["status"] = "spare"
                    pending.remove(cid_str)
                    logger.info(f"cleanup: group {cid} is now spare")
                else:
                    logger.warning(f"cleanup: group {cid} failed, will retry")
            for req in list(link_reqs):
                try:
                    cid = int(req.get("chat_id", 0))
                    admin_id = int(req.get("admin_id", 0))
                except (ValueError, TypeError, AttributeError):
                    link_reqs.remove(req)
                    continue
                try:
                    entity = await user_client.get_entity(cid)
                    inv = await user_client(ExportChatInviteRequest(peer=entity))
                    link = getattr(inv, "link", None)
                    if link and admin_id:
                        await bot_client.send_message(
                            admin_id,
                            f"🔗 <b>Fresh invite link</b>\n\n{link}\n\n"
                            f"(Group ID: <code>{cid}</code>)",
                            parse_mode="html",
                        )
                        logger.info(f"link: fresh invite sent for group {cid}")
                    else:
                        logger.warning(f"link: export failed for group {cid}")
                except Exception as e:
                    logger.warning(f"link: request for {cid} failed: {e}")
                link_reqs.remove(req)
            _save_pool(pool)
        except Exception as e:
            logger.error(f"cleanup loop error: {e}")
        await asyncio.sleep(600)  # check every 10 min


# ==================== BULK CLEANUP (admin-triggered) ====================

async def _bulk_cleanup_unused_groups():
    """Immediately recycle EVERY unpaid/unused deal group (admin-triggered).

    Paid deals (payment_confirmed_at) are never touched. Groups with
    activity in the last 60 minutes are skipped for safety.
    Returns (cleaned, skipped_paid, skipped_active, error).
    """
    if not USER_CLIENT_AVAILABLE:
        return (0, 0, 0, "user client unavailable")
    if not await ensure_client_connected(user_client, "NEW"):
        return (0, 0, 0, "could not connect user client")
    pool = _load_pool()
    # --- discover deal groups not yet tracked ---
    try:
        async for d in user_client.iter_dialogs():
            ent = d.entity
            if not getattr(ent, "megagroup", False):
                continue
            title = getattr(ent, "title", "") or ""
            if not title.startswith(GROUP_BASE_NAME):
                continue
            cid = _full_chat_id(ent.id)
            if str(cid) not in pool["groups"]:
                created = getattr(ent, "date", None)
                pool["groups"][str(cid)] = {
                    "created_at": created.timestamp() if created else time.time(),
                    "status": "spare" if title.endswith("\u267b\ufe0f") else "active",
                    "deal_no": None,
                    "title": title,
                    "addr_gen": 0,
                }
    except Exception as e:
        logger.error(f"bulk cleanup discovery failed: {e}")
    deal_states = _load_deal_states_file()
    now = time.time()
    cleaned, skipped_paid, skipped_active = 0, 0, 0
    for cid_str, g in list(pool["groups"].items()):
        if g.get("status") == "spare":
            continue
        cid = int(cid_str)
        st = deal_states.get(cid_str)
        if st and st.get("payment_confirmed_at"):
            skipped_paid += 1
            continue  # paid deal - never touch
        try:
            msgs = await user_client.get_messages(cid, limit=1)
        except Exception:
            continue
        last_ts = msgs[0].date.timestamp() if msgs else g.get("created_at", 0)
        if now - last_ts < 3600:
            skipped_active += 1
            continue  # active within the last hour - safety skip
        title = g.get("title") or GROUP_BASE_NAME
        logger.info(f"bulk cleanup: recycling {cid} '{title}'")
        if await _recycle_group_to_spare(cid, title, keep_admins=True):
            g["status"] = "spare"
            cleaned += 1
        else:
            logger.warning(f"bulk cleanup: {cid} failed, left as-is")
    _save_pool(pool)
    return (cleaned, skipped_paid, skipped_active, None)


@bot_client.on(events.NewMessage(pattern=r"^/cleanup_groups$"))
async def cleanup_groups_handler(event):
    """Admin-only: immediately recycle all unpaid/unused deal groups."""
    try:
        admin_ok = False
        try:
            admin_ent = await bot_client.get_entity(SUPPORT_CONTACT)
            admin_ok = event.sender_id == admin_ent.id
        except Exception:
            pass
        if not admin_ok:
            await event.respond("\u26d4 <b>Admin only.</b>", parse_mode="html")
            return
        prog = await event.respond("\U0001f9f9 <b>Scanning deal groups...</b>", parse_mode="html")
        cleaned, skipped_paid, skipped_active, err = await _bulk_cleanup_unused_groups()
        if err:
            await prog.edit(f"\u274c <b>Cleanup failed:</b> {html.escape(err)}", parse_mode="html")
            return
        await prog.edit(
            f"\u2705 <b>Cleanup done!</b>\n\n"
            f"\U0001f9f9 Recycled to spare: <b>{cleaned}</b>\n"
            f"\U0001f4b0 Skipped (paid deals): <b>{skipped_paid}</b>\n"
            f"\u23f3 Skipped (active in last hour): <b>{skipped_active}</b>\n\n"
            f"Recycled groups will be reused for new deals with fresh links.\n"
            f"Admins were kept; all other users were removed.",
            parse_mode="html",
        )
    except Exception as e:
        logger.error(f"cleanup_groups handler failed: {e}")


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
    except UserAlreadyParticipantError:
        logger.info(f"Already a member ({label}): {username_or_id} — proceeding to promote")
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
# Deletes messages from senders whose username or display name matches spam pattern
# (case-insensitive). Catches: xxin, xxxin, xxin7, xinxin, etc.
# Requires the bot to be admin with delete rights in the group.
# Set SPAM_NAME_PATTERN env var (regex) to customize; empty = all groups watched.
SPAM_NAME_PATTERN = re.compile(os.environ.get("SPAM_NAME_PATTERN", r"x{2,}i*n|xinxin|xxin\d*"), re.IGNORECASE)
_env_spam_groups = os.environ.get("SPAM_DELETE_GROUPS", "").strip()
SPAM_DELETE_GROUPS = [s.strip() for s in _env_spam_groups.split(",") if s.strip()]

@user_client.on(events.ChatAction)
async def auto_promote_service_admin_on_join(event):
    """If one of our service accounts joins a deal group via invite link
    (e.g. after a privacy-blocked direct invite), auto-promote to admin."""
    try:
        if not (event.user_joined or event.user_added):
            return
        chat = await event.get_chat()
        title = getattr(chat, "title", "") or ""
        if not title.startswith(GROUP_BASE_NAME):
            return
        user = await event.get_user()
        uname = (getattr(user, "username", "") or "").lower()
        if uname not in ("balochbuyer", "balochhelpbot", "reall_baloch"):
            return
        await user_client(
            EditAdminRequest(
                channel=event.chat_id,
                user_id=user.id,
                admin_rights=make_admin_rights(),
                rank="Escrow Admin",
            )
        )
        logger.info(f"Auto-promoted @{uname} to admin on join in '{title}'")
    except Exception as e:
        logger.warning(f"auto_promote_service_admin_on_join failed: {type(e).__name__}: {e}")


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
        username = (getattr(sender, 'username', '') or '')
        first_name = getattr(sender, 'first_name', '') or ''
        last_name = getattr(sender, 'last_name', '') or ''
        full_name = f"{first_name} {last_name}".strip()
        # Check BOTH username and display name for xxin (bots often hide it in name)
        if SPAM_NAME_PATTERN.search(username) or SPAM_NAME_PATTERN.search(full_name):
            await event.delete()
            logger.info(
                f"Deleted XXIN spam from @{username or sender.id} in chat {event.chat_id}"
            )
    except Exception as e:
        logger.warning(f"Failed to delete XXIN spam: {e}")


# ==================== BOT HANDLERS ====================

@bot_client.on(events.NewMessage(pattern=r"^/start(\s.*)?$"))
async def start_handler(event):
    """Handle /start command (with or without deep-link payload, e.g. ?start=1)."""
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
        # Welcome animation, sent above the welcome text. Optional file:
        # if it is missing or fails, /start still sends the welcome message.
        try:
            if os.path.exists(ANIMATION_FILE):
                await event.respond(file=ANIMATION_FILE)
        except Exception as anim_e:
            logger.error(f"Welcome animation failed: {anim_e}")
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

        # ---------- STEP 1: reuse spare group or create supergroup ----------
        group_num = await peek_next_group_number()
        group_title = f"{GROUP_BASE_NAME} {group_num}"
        created_group = None
        group_id = None
        reused_spare_id = None
        revoked_links = set()

        spare_chat_id = _get_spare_group()
        if spare_chat_id:
            try:
                _ent = await user_client.get_entity(spare_chat_id)
                logger.info(f"Trying spare group reuse: {spare_chat_id}")
                _revoked = await _prepare_spare_group(_ent, group_title)
                if _revoked is not None:
                    created_group = _ent
                    group_id = _ent.id
                    reused_spare_id = spare_chat_id
                    revoked_links = _revoked
                    logger.info(f"REUSED spare group '{group_title}' ({spare_chat_id})")
            except Exception as e:
                logger.error(f"Spare reuse failed, creating new: {e}")
                created_group = None

        if created_group is None:
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
                _register_group(_full_chat_id(group_id), group_num, group_title)
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

        # Number is consumed only after successful creation/reuse
        await commit_group_number(group_num)
        if reused_spare_id:
            _mark_active(reused_spare_id, group_num, group_title)
            _needs_reset_add(reused_spare_id)

        # ---------- STEP 2: invite + promote helper bot ----------
        bot_admin_ok = await invite_and_promote(
            user_client, created_group, ADD_BOT_USERNAME, "Escrow Admin", "helper bot"
        )

        # ---------- STEP 3: export invite link ----------
        # REUSED groups: exporting through the creator account keeps
        # returning the group's old PERMANENT link - the one revoked in
        # bulk during spare prep - so users kept seeing "this invite link
        # has expired". Fix: force Telegram to REGENERATE the permanent
        # link (legacy_revoke_permanent), and never hand out a link that
        # sits in the revoked set - every candidate is verified and the
        # export retries until a live link comes back. If none does, the
        # user gets the honest "invite link failed" message below instead
        # of a dead link.
        async def _revoked_now():
            """Links currently known revoked for this group."""
            bad = set(revoked_links)
            try:
                me = await user_client.get_me()
                invs = await user_client(GetExportedChatInvitesRequest(
                    peer=created_group, admin_id=me.id, limit=100))
                for iv in getattr(invs, "invites", []) or []:
                    if getattr(iv, "revoked", False) and getattr(iv, "link", None):
                        bad.add(iv.link)
            except Exception as e:
                logger.warning(f"revoked-link scan failed: {e}")
            return bad

        async def _link_opens(link):
            """Ground-truth check: ask Telegram the way a user's app does
            when the link is tapped (messages.checkChatInvite). Catches
            links that look fine in the export list but are dead on tap."""
            try:
                h = link.split("/")[-1].replace("+", "")
                await user_client(CheckChatInviteRequest(h))
                return True
            except Exception as e:
                logger.warning(f"invite link does not open on check: {e}")
                return False

        async def _export_invite():
            if not reused_spare_id:
                inv = await user_client(ExportChatInviteRequest(peer=created_group))
                return getattr(inv, "link", None)
            attempts = (
                {"legacy_revoke_permanent": True},  # regenerate permanent link
                {"title": f"Deal {group_num}"},     # mint a brand-new link
                {},                                 # bare export (last resort)
            )
            for _round in range(3):
                bad = await _revoked_now()
                for kwargs in attempts:
                    try:
                        inv = await user_client(ExportChatInviteRequest(
                            peer=created_group, **kwargs))
                    except TypeError:
                        continue  # telethon too old for this parameter
                    except Exception as e:
                        logger.warning(f"invite export {kwargs} failed: {e}")
                        continue
                    link = getattr(inv, "link", None)
                    if link and link not in bad and await _link_opens(link):
                        return link
                    logger.warning(f"export returned dead link, retrying: {link}")
                await asyncio.sleep(2)
            return None

        invite_link = None
        try:
            invite_link = await _export_invite()
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
        if not buyer_admin_ok:
            # Direct add failed (usually the account's privacy settings block
            # group invites from non-contacts). DM the invite link instead —
            # auto_promote_service_admin_on_join promotes them on join.
            try:
                _dm = (
                    f"🔔 <b>New escrow group needs you as admin:</b> {group_title}\n\n"
                    f"Direct add failed (privacy settings?).\n"
                    f"Join here — you will be auto-promoted to admin:\n{invite_link}"
                )
                try:
                    await bot_client.send_message(SUPPORT_USERNAME, _dm, parse_mode="html")
                except Exception:
                    await user_client.send_message(SUPPORT_USERNAME, _dm, parse_mode="html")
                logger.info(f"Sent admin-join DM to @{SUPPORT_USERNAME} for {group_title}")
            except Exception as e:
                logger.error(f"Could not DM @{SUPPORT_USERNAME}: {e}")

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

        # Store the active invite link in the pool — this link is NEVER revoked
        # while the deal is active, so users never see "this link expired".
        try:
            _pool = _load_pool()
            _gkey = str(_full_chat_id(group_id))
            _g = _pool.get("groups", {}).get(_gkey)
            if isinstance(_g, dict):
                _g["active_link"] = invite_link
                _save_pool(_pool)
                logger.info(f"Stored active_link for group {_gkey}")
        except Exception as e:
            logger.warning(f"Could not store active_link: {e}")

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
_recycle_task = None
_cleanup_task = None


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

    global _reminder_task, _broadcast_task, _recycle_task, _cleanup_task
    _reminder_task = asyncio.create_task(reminder_loop())
    _broadcast_task = asyncio.create_task(broadcast_watcher())
    _recycle_task = asyncio.create_task(recycle_loop())
    _cleanup_task = asyncio.create_task(cleanup_loop())

    logger.info("BOT IS NOW RUNNING AND READY")
    logger.info(f"Deal Handler Bot : @{ADD_BOT_USERNAME}")
    logger.info(f"Support Contact  : {SUPPORT_CONTACT}")

    try:
        await bot_client.run_until_disconnected()
    finally:
        for _t in (_reminder_task, globals().get("_broadcast_task"), globals().get("_recycle_task"),
                   globals().get("_cleanup_task")):
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
