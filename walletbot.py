#!/usr/bin/env python3
"""
Personal Wallet Bot — Multi-user USDT (TRON TRC20 default + BNB Smart Chain BEP20).
- Each user gets a unique deposit address per chain (BIP44 HD, same index).
- Admin notified on every deposit (username + name + amount).
- Withdraw/payout: admin only. Gas auto-funded, leftover swept back.
"""
import asyncio
import json
import logging
import os
import sys

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("walletbot")

BOT_TOKEN = os.environ.get("WALLET_BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("WALLET_ADMIN_IDS", "").split(",") if x.strip().isdigit()}

USE_TRON = os.environ.get("WALLET_USE_TRON", "false").lower() == "true"
TRON_PRIVATE_KEY = os.environ.get("TRON_PRIVATE_KEY", "").strip()

try:
    import tron_pay
    TRON_AVAILABLE = bool(TRON_PRIVATE_KEY) and bool(os.environ.get("WALLET_SEED_PHRASE", "").strip())
except ImportError:
    TRON_AVAILABLE = False
    tron_pay = None

# BNB Smart Chain (BEP20 USDT) — optional second network, TRON stays default.
# Same WALLET_SEED_PHRASE derives BSC addresses; hot wallet = same key bytes.
USE_BSC = os.environ.get("WALLET_USE_BSC", "true").lower() == "true"

try:
    import bsc_pay
    BSC_AVAILABLE = bool(os.environ.get("WALLET_SEED_PHRASE", "").strip())
except ImportError:
    BSC_AVAILABLE = False
    bsc_pay = None

BSC_ENABLED = USE_BSC and BSC_AVAILABLE

CHAIN_LABEL = {"tron": "TRON (TRC20)", "bsc": "BNB Smart Chain (BEP20)"}
GAS_LABEL = {"tron": "TRX", "bsc": "BNB"}
EXPLORER_TX = {
    "tron": "https://tronscan.org/#/transaction/",
    "bsc": "https://bscscan.com/tx/",
}

if not BOT_TOKEN:
    logger.error("WALLET_BOT_TOKEN not set!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

USERS_FILE = "/root/wallet_users.json"
user_state: dict[int, dict] = {}


def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def _chain_of(user_id: int) -> str:
    """Selected chain for a user; TRON is the default."""
    try:
        users = load_users()
        c = users.get(str(user_id), {}).get("chain", "tron")
    except Exception:
        c = "tron"
    if c == "bsc" and not BSC_ENABLED:
        return "tron"
    return c


def _set_chain(user_id: int, chain: str):
    users = load_users()
    uid = str(user_id)
    if uid in users:
        users[uid]["chain"] = chain
        save_users(users)


def _ensure_bsc_address(rec: dict) -> str:
    """Lazy-fill BSC address for users created before BNB support."""
    if not rec.get("bsc_address") and BSC_ENABLED:
        try:
            rec["bsc_address"] = bsc_pay.get_user_deposit_address(rec["index"])
        except Exception as e:
            logger.warning(f"BSC address derivation failed: {e}")
            rec["bsc_address"] = ""
    return rec.get("bsc_address", "")


def _addr_for(rec: dict, chain: str) -> str:
    if chain == "bsc":
        return _ensure_bsc_address(rec)
    return rec.get("address", "")


def _is_tron_address(text: str) -> bool:
    t = text.strip()
    return t.startswith("T") and len(t) == 34


def _is_bsc_address(text: str) -> bool:
    t = text.strip()
    if not (t.startswith("0x") and len(t) == 42):
        return False
    try:
        int(t[2:], 16)
        return True
    except ValueError:
        return False


def _valid_dest(text: str, chain: str) -> bool:
    return _is_bsc_address(text) if chain == "bsc" else _is_tron_address(text)


def _dest_kind_label(chain: str) -> str:
    return "BSC (0x...)" if chain == "bsc" else "TRON (T...)"


def load_users() -> dict:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_users(users: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f)


def get_or_create_user(user_id: int, username: str, first_name: str) -> dict:
    """Get user record, creating with unique HD address if new."""
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        # Assign next index
        used = {u.get("index", 0) for u in users.values()}
        idx = 0
        while idx in used:
            idx += 1
        address = tron_pay.get_user_deposit_address(idx)
        bsc_address = ""
        if BSC_ENABLED:
            try:
                bsc_address = bsc_pay.get_user_deposit_address(idx)
            except Exception as e:
                logger.warning(f"BSC address derivation failed for new user: {e}")
        users[uid] = {
            "index": idx,
            "address": address,
            "bsc_address": bsc_address,
            "chain": "tron",  # TRON is the default network
            "username": username or "",
            "first_name": first_name or "",
            "seen_txids": [],
        }
        save_users(users)
        logger.info(f"New wallet user {user_id} -> index {idx}, {address}")
    else:
        # Update username/name if changed
        if users[uid].get("username") != (username or "") or users[uid].get("first_name") != (first_name or ""):
            users[uid]["username"] = username or ""
            users[uid]["first_name"] = first_name or ""
            save_users(users)
        # Lazy-fill BSC address for users created before BNB support
        if BSC_ENABLED and not users[uid].get("bsc_address"):
            try:
                users[uid]["bsc_address"] = bsc_pay.get_user_deposit_address(users[uid]["index"])
                save_users(users)
            except Exception as e:
                logger.warning(f"BSC lazy-fill failed: {e}")
    return users[uid]


def _chain_toggle_row(chain: str):
    if not BSC_ENABLED:
        return []
    other = "BNB (BEP20)" if chain == "tron" else "TRON (TRC20)"
    return [[InlineKeyboardButton(
        text=f"⛓ Network: {CHAIN_LABEL[chain]} — switch to {other}",
        callback_data="chain_toggle")]]


def user_menu(chain: str = "tron") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_chain_toggle_row(chain) + [
        [InlineKeyboardButton(text="📥 My Deposit Address", callback_data="u_address")],
        [InlineKeyboardButton(text="💰 My Balance", callback_data="u_balance")],
    ])


def admin_menu(chain: str = "tron") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_chain_toggle_row(chain) + [
        [InlineKeyboardButton(text="📥 My Deposit Address", callback_data="u_address")],
        [InlineKeyboardButton(text="💰 My Balance", callback_data="u_balance")],
        [InlineKeyboardButton(text="💸 Send (Admin)", callback_data="w_send")],
        [InlineKeyboardButton(text="🔄 Swap USDT→TRX", callback_data="a_swap")],
        [InlineKeyboardButton(text="👥 Users", callback_data="a_users")],
        [InlineKeyboardButton(text="🤝 Deal Funds", callback_data="a_deals")],
        [InlineKeyboardButton(text="💰 Withdraw ALL → My Wallet", callback_data="a_withdraw_all")],
        [InlineKeyboardButton(text="📦 Backup Deals", callback_data="a_backup")],
        [InlineKeyboardButton(text="🧹 Recover TRX", callback_data="a_sweep")],
    ])


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not (USE_TRON and TRON_AVAILABLE):
        await message.answer("⛔ Wallet not configured.")
        return
    u = message.from_user
    rec = await asyncio.get_event_loop().run_in_executor(
        None, get_or_create_user, u.id, u.username, u.first_name)
    chain = _chain_of(u.id)
    addr = _addr_for(rec, chain)
    menu = admin_menu(chain) if _is_admin(u.id) else user_menu(chain)
    role = " (Admin)" if _is_admin(u.id) else ""
    await message.answer(
        f"👛 <b>My Wallet</b>{role}\n\n"
        f"Network: <b>{CHAIN_LABEL[chain]}</b> | Token: <b>USDT</b>\n\n"
        f"Your deposit address:\n<code>{addr}</code>\n\n"
        f"Choose an action:",
        parse_mode="HTML",
        reply_markup=menu,
    )


@dp.callback_query(F.data == "chain_toggle")
async def cb_chain_toggle(callback: types.CallbackQuery):
    """Switch between TRON and BNB Smart Chain (TRON stays default)."""
    await callback.answer()
    if not BSC_ENABLED:
        await callback.message.answer("⛔ BNB chain is not configured on this bot.")
        return
    u = callback.from_user
    loop = asyncio.get_event_loop()
    rec = await loop.run_in_executor(
        None, get_or_create_user, u.id, u.username, u.first_name)
    new_chain = "bsc" if _chain_of(u.id) == "tron" else "tron"
    await loop.run_in_executor(None, _set_chain, u.id, new_chain)
    addr = await loop.run_in_executor(None, _addr_for, rec, new_chain)
    menu = admin_menu(new_chain) if _is_admin(u.id) else user_menu(new_chain)
    await callback.message.answer(
        f"⛓ Network switched to <b>{CHAIN_LABEL[new_chain]}</b>\n\n"
        f"Your deposit address:\n<code>{addr}</code>",
        parse_mode="HTML",
        reply_markup=menu,
    )


@dp.callback_query(F.data == "u_address")
async def cb_u_address(callback: types.CallbackQuery):
    await callback.answer()
    u = callback.from_user
    loop = asyncio.get_event_loop()
    rec = await loop.run_in_executor(
        None, get_or_create_user, u.id, u.username, u.first_name)
    chain = _chain_of(u.id)
    addr = await loop.run_in_executor(None, _addr_for, rec, chain)
    await callback.message.answer(
        f"📥 <b>Your USDT Deposit Address</b> ({CHAIN_LABEL[chain]})\n\n"
        f"<code>{addr}</code>\n\n"
        f"Send USDT here. Admin is notified automatically on deposit!",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "u_balance")
async def cb_u_balance(callback: types.CallbackQuery):
    await callback.answer("Checking...")
    u = callback.from_user
    loop = asyncio.get_event_loop()
    rec = await loop.run_in_executor(
        None, get_or_create_user, u.id, u.username, u.first_name)
    chain = _chain_of(u.id)
    addr = await loop.run_in_executor(None, _addr_for, rec, chain)
    gas_label = GAS_LABEL[chain]
    if chain == "bsc":
        bal = await loop.run_in_executor(None, bsc_pay.get_usdt_balance, addr)
        gas_bal = await loop.run_in_executor(None, bsc_pay.get_bnb_balance, addr)
    else:
        bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, addr)
        gas_bal = await loop.run_in_executor(None, tron_pay.get_trx_balance, addr)
    # Admin sees total across all funds (both chains)
    if _is_admin(u.id):
        import json
        def _totals():
            total = bal
            total_gas = gas_bal
            # Deal escrow funds (TRON only)
            try:
                with open("/root/deal_states.json") as f:
                    states = json.load(f)
                for s in states.values():
                    a = s.get("tron_deposit_address")
                    if a and a != rec["address"]:
                        total += tron_pay.get_usdt_balance(a)
                        total_gas += tron_pay.get_trx_balance(a)
            except Exception:
                pass
            # User deposits (same chain as selected)
            try:
                users = load_users()
                for uid, urec in users.items():
                    a = _addr_for(urec, chain)
                    if a and a != addr:
                        if chain == "bsc":
                            total += bsc_pay.get_usdt_balance(a)
                            total_gas += bsc_pay.get_bnb_balance(a)
                        else:
                            total += tron_pay.get_usdt_balance(a)
                            total_gas += tron_pay.get_trx_balance(a)
            except Exception:
                pass
            return total, total_gas
        total, total_gas = await loop.run_in_executor(None, _totals)
        # Hot wallet gas on the other chain (so admin knows to top up)
        extra = ""
        if BSC_ENABLED:
            def _hot():
                try:
                    return (bsc_pay.get_usdt_balance(bsc_pay.get_hot_wallet_address()),
                            bsc_pay.get_bnb_balance(bsc_pay.get_hot_wallet_address()))
                except Exception:
                    return (0.0, 0.0)
            hot_usdt, hot_bnb = await loop.run_in_executor(None, _hot)
            hot_trx = tron_pay.get_trx_balance(tron_pay.get_hot_wallet_address())
            extra = (f"\n⛽ Hot wallet gas — TRX: <b>{hot_trx:.1f}</b> | BNB: <b>{hot_bnb:.4f}</b>\n"
                     f"<i>Top up BNB at</i> <code>{bsc_pay.get_hot_wallet_address()}</code>")
        await callback.message.answer(
            f"💰 <b>Total Balance</b> ({CHAIN_LABEL[chain]})\n\n"
            f"My Wallet: <b>{bal:.2f}</b> USDT | <b>{gas_bal:.2f}</b> {gas_label}\n"
            f"All Funds: <b>{total:.2f}</b> USDT | <b>{total_gas:.2f}</b> {gas_label}\n\n"
            f"<i>Includes deal escrow + user deposits</i>{extra}",
            parse_mode="HTML",
        )
        return
    await callback.message.answer(
        f"💰 <b>Your Balance</b> ({CHAIN_LABEL[chain]})\n\nUSDT: <b>{bal:.2f}</b>\n{gas_label}: <b>{gas_bal:.4f}</b>",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "a_users")
async def cb_a_users(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    users = load_users()
    if not users:
        await callback.message.answer("👥 No users yet.")
        return
    loop = asyncio.get_event_loop()
    chain = _chain_of(callback.from_user.id)
    lines = [f"👥 <b>Users Wallet</b> ({CHAIN_LABEL[chain]})\n"]
    kb_rows = []
    for uid, rec in list(users.items())[:20]:
        addr = await loop.run_in_executor(None, _addr_for, rec, chain)
        if not addr:
            continue
        if chain == "bsc":
            bal = await loop.run_in_executor(None, bsc_pay.get_usdt_balance, addr)
        else:
            bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, addr)
        un = f"@{rec['username']}" if rec.get("username") else rec.get("first_name", uid)
        lines.append(f"• {un} — <b>{bal:.2f}</b> USDT\n  <code>{addr}</code>")
        if bal > 0.01:
            kb_rows.append([InlineKeyboardButton(
                text=f"💸 Withdraw {bal:.2f} USDT ({un})",
                callback_data=f"user_wd_{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("user_wd_") & ~F.data.startswith("user_wd_confirm_"))
async def cb_user_withdraw(callback: types.CallbackQuery):
    """Start withdraw from a user's deposit address (admin only). Gas auto-funded from hot wallet."""
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.data[len("user_wd_"):]
    users = load_users()
    rec = users.get(uid)
    if not rec:
        await callback.message.answer("❌ User not found.")
        return
    chain = _chain_of(callback.from_user.id)
    addr = _addr_for(rec, chain)
    user_state[callback.from_user.id] = {"step": "user_withdraw", "target_uid": uid, "chain": chain}
    un = f"@{rec['username']}" if rec.get("username") else rec.get("first_name", uid)
    await callback.message.answer(
        f"💸 <b>Withdraw from {un}</b> ({CHAIN_LABEL[chain]})\n\n"
        f"From: <code>{addr}</code>\n\n"
        f"Send the destination {_dest_kind_label(chain)} address:",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("user_wd_confirm_"))
async def cb_user_wd_confirm(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    st = user_state.get(uid, {})
    chain = st.get("chain", "tron")
    if callback.data == "user_wd_confirm_no":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu(chain))
        return
    await callback.message.answer("⏳ Funding gas from hot wallet + broadcasting...")
    loop = asyncio.get_event_loop()
    def _do():
        if chain == "bsc":
            pk_hex = bsc_pay.get_user_private_key_hex(st["user_index"])
            return bsc_pay.send_usdt_from_with_gas(pk_hex, st["user_addr"], st["address"], st["amount"])
        pk = tron_pay.get_user_private_key(st["user_index"])
        return tron_pay.send_usdt_from_with_gas(pk, st["address"], st["amount"])
    result = await loop.run_in_executor(None, _do)
    user_state.pop(uid, None)
    if result.get("ok"):
        txid = result.get("txid", "")
        await callback.message.answer(
            f"✅ <b>Withdrawn!</b> ({CHAIN_LABEL[chain]})\n\nAmount: <b>{st.get('amount', 0)} USDT</b>\n"
            f"TxID: <code>{txid}</code>\n"
            f"{EXPLORER_TX[chain]}{txid}",
            parse_mode="HTML", reply_markup=admin_menu(chain))
    else:
        await callback.message.answer(
            f"❌ Failed: {result.get('error', '?')}\n\n"
            f"Tip: hot wallet needs enough {GAS_LABEL[chain]} for gas!",
            parse_mode="HTML", reply_markup=admin_menu(chain))


@dp.callback_query(F.data == "a_deals")
async def cb_a_deals(callback: types.CallbackQuery):
    """Show all deal escrow addresses with balances (admin only) — TRON + BSC."""
    await callback.answer("Loading...")
    if not _is_admin(callback.from_user.id):
        return
    import json
    loop = asyncio.get_event_loop()
    def _load():
        try:
            with open("/root/deal_states.json") as f:
                return json.load(f)
        except Exception:
            return {}
    states = await loop.run_in_executor(None, _load)
    # (chat_id, chain, address, hd_index, stage)
    deals = []
    for cid, s in states.items():
        if s.get("tron_deposit_address"):
            deals.append((cid, "tron", s["tron_deposit_address"],
                          s.get("hd_index", s.get("tron_hd_index")), s.get("stage", "?")))
        if s.get("bsc_deposit_address"):
            deals.append((cid, "bsc", s["bsc_deposit_address"],
                          s.get("hd_index"), s.get("stage", "?")))
    if not deals:
        await callback.message.answer("🤝 No deal escrow addresses yet.")
        return
    lines = ["🤝 <b>Deal Escrow Funds</b>\n"]
    kb_rows = []
    for cid, chain, addr, hd_index, stage in deals[-12:]:  # last 12
        if chain == "bsc":
            bal = await loop.run_in_executor(None, bsc_pay.get_usdt_balance, addr)
            gas = await loop.run_in_executor(None, bsc_pay.get_bnb_balance, addr)
            gas_warn = " ⚠️" if gas < 0.004 else ""
            gas_txt = f"{gas:.4f} BNB{gas_warn}"
        else:
            bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, addr)
            gas = await loop.run_in_executor(None, tron_pay.get_trx_balance, addr)
            gas_warn = " ⚠️" if gas < 5 else ""
            gas_txt = f"{gas:.1f} TRX{gas_warn}"
        no_key = " 🔑❌" if hd_index is None else ""
        lines.append(f"• {CHAIN_LABEL[chain]} Chat {cid} — <b>{bal:.2f}</b> USDT | <b>{gas_txt}</b> ({stage}){no_key}\n  <code>{addr}</code>")
        if bal > 0.01:
            kb_rows.append([InlineKeyboardButton(
                text=f"💸 Withdraw {bal:.2f} ({CHAIN_LABEL[chain]}) {cid}",
                callback_data=f"deal_wd_{chain}_{cid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("deal_wd_") & ~F.data.startswith("deal_wd_confirm_"))
async def cb_deal_withdraw(callback: types.CallbackQuery):
    """Start manual withdraw from a deal address (admin only)."""
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    rest = callback.data[len("deal_wd_"):]  # "{chain}_{chat_id}"
    chain, _, chat_id = rest.partition("_")
    if chain not in ("tron", "bsc"):
        chain, chat_id = "tron", rest  # back-compat: old "deal_wd_{cid}" buttons
    user_state[callback.from_user.id] = {"step": "deal_withdraw", "deal_chat_id": chat_id, "chain": chain}
    await callback.message.answer(
        f"💸 <b>Withdraw from Deal {chat_id}</b> ({CHAIN_LABEL[chain]})\n\nSend the destination {_dest_kind_label(chain)} address:",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("deal_wd_confirm_"))
async def cb_deal_wd_confirm(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    st = user_state.get(uid, {})
    chain = st.get("chain", "tron")
    if callback.data == "deal_wd_confirm_no":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu(chain))
        return
    await callback.message.answer("⏳ Funding gas from hot wallet + broadcasting...")
    loop = asyncio.get_event_loop()
    def _do():
        if chain == "bsc":
            if st.get("hd_index") is None:
                # v1-style deal: funds sit on the hot wallet itself
                return bsc_pay.send_usdt(st["address"], st["amount"])
            pk_hex = bsc_pay.get_user_private_key_hex(st["hd_index"])
            return bsc_pay.send_usdt_from_with_gas(pk_hex, st["deal_addr"], st["address"], st["amount"])
        if st.get("hd_index") is None:
            # v1-style deal: funds sit on the hot wallet itself
            return tron_pay.send_usdt(st["address"], st["amount"])
        pk = tron_pay.get_user_private_key(st["hd_index"])
        return tron_pay.send_usdt_from_with_gas(pk, st["address"], st["amount"])
    result = await loop.run_in_executor(None, _do)
    user_state.pop(uid, None)
    if result.get("ok"):
        txid = result.get("txid", "")
        await callback.message.answer(
            f"✅ <b>Withdrawn!</b> ({CHAIN_LABEL[chain]})\n\nTxID: <code>{txid}</code>\n"
            f"{EXPLORER_TX[chain]}{txid}",
            parse_mode="HTML", reply_markup=admin_menu(chain))
    else:
        await callback.message.answer(
            f"❌ Failed: {result.get('error', '?')}\n\n"
            f"Tip: hot wallet needs enough {GAS_LABEL[chain]} for gas!",
            parse_mode="HTML", reply_markup=admin_menu(chain))


@dp.callback_query(F.data == "a_sweep")
async def cb_a_sweep(callback: types.CallbackQuery):
    """Sweep leftover TRX from all deal addresses back to hot wallet."""
    await callback.answer("Sweeping...")
    if not _is_admin(callback.from_user.id):
        return
    import json
    await callback.message.answer("🧹 <b>Sweeping leftover TRX...</b>", parse_mode="HTML")
    loop = asyncio.get_event_loop()
    def _sweep_all():
        results = []
        try:
            with open("/root/deal_states.json") as f:
                states = json.load(f)
        except Exception:
            return [("error", "No deal data")]
        hot_addr = tron_pay._get_master_key().public_key.to_base58check_address()
        for cid, s in states.items():
            addr = s.get("tron_deposit_address")
            idx = s.get("tron_hd_index")
            if not addr or not idx:
                continue
            try:
                bal = tron_pay.get_trx_balance(addr)
                if bal > 1.5:
                    sweep_amt = round(bal - 1.0, 2)
                    pk = tron_pay.get_user_private_key(idx)
                    client = tron_pay._get_client()
                    txn = (
                        client.trx.transfer(addr, hot_addr, int(sweep_amt * 1_000_000))
                        .build()
                        .sign(pk)
                    )
                    r = txn.broadcast()
                    results.append((addr, sweep_amt, "ok"))
                else:
                    results.append((addr, bal, "skip"))
            except Exception as e:
                results.append((addr, 0, f"fail: {e}"))
        return results
    results = await loop.run_in_executor(None, _sweep_all)
    total = sum(r[1] for r in results if r[2] == "ok")
    lines = [f"🧹 <b>Sweep Complete!</b>\n", f"Recovered: <b>{total:.2f} TRX</b>\n"]
    for addr, amt, st in results:
        if st == "ok":
            lines.append(f"✅ {addr[:10]}...: {amt:.2f} TRX")
    await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                      reply_markup=admin_menu(_chain_of(callback.from_user.id)))


# ============================================================
# MASTER SWEEP — Withdraw ALL funds (users + deals, both chains)
# to the hot wallet ("my wallet"). Bot takes ZERO fee; only
# network gas applies (auto-funded, leftover swept back).
# ============================================================
SWEEP_DUST = 1.0  # skip balances below this (gas would eat them)


def _scan_all_funds():
    """Blocking scan: returns list of (kind, label, chain, addr, key_kind, key_index, bal)."""
    import json
    items = []
    try:
        users = load_users()
    except Exception:
        users = {}
    try:
        with open("/root/deal_states.json") as f:
            states = json.load(f)
    except Exception:
        states = {}
    # --- user deposits (both chains) ---
    for uid, rec in users.items():
        idx = rec.get("index")
        un = f"@{rec['username']}" if rec.get("username") else rec.get("first_name", uid)
        taddr = rec.get("address", "")
        if taddr and idx is not None:
            try:
                bal = tron_pay.get_usdt_balance(taddr)
            except Exception:
                bal = 0
            items.append(("user", f"User {un}", "tron", taddr, "user", idx, bal))
        baddr = rec.get("bsc_address") or ""
        if not baddr and idx is not None and BSC_ENABLED:
            try:
                baddr = bsc_pay.get_user_deposit_address(idx)
            except Exception:
                baddr = ""
        if baddr and idx is not None:
            try:
                bal = bsc_pay.get_usdt_balance(baddr)
            except Exception:
                bal = 0
            items.append(("user", f"User {un}", "bsc", baddr, "user", idx, bal))
    # --- deal escrow (both chains) ---
    for cid, s in states.items():
        taddr = s.get("tron_deposit_address")
        if taddr:
            idx = s.get("hd_index", s.get("tron_hd_index"))
            try:
                bal = tron_pay.get_usdt_balance(taddr)
            except Exception:
                bal = 0
            items.append(("deal", f"Deal {cid}", "tron", taddr, "deal", idx, bal))
        baddr = s.get("bsc_deposit_address")
        if baddr:
            idx = s.get("hd_index")
            try:
                bal = bsc_pay.get_usdt_balance(baddr)
            except Exception:
                bal = 0
            items.append(("deal", f"Deal {cid}", "bsc", baddr, "deal", idx, bal))
    return items


@dp.callback_query(F.data == "a_withdraw_all")
async def cb_a_withdraw_all(callback: types.CallbackQuery):
    """Scan everything and ask for confirmation before the master sweep."""
    await callback.answer("Scanning all funds...")
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    await callback.message.answer("🔍 <b>Scanning all addresses (TRON + BSC)...</b>", parse_mode="HTML")
    loop = asyncio.get_event_loop()
    items = await loop.run_in_executor(None, _scan_all_funds)
    movable = [it for it in items if it[6] >= SWEEP_DUST]
    dust = [it for it in items if 0 < it[6] < SWEEP_DUST]
    if not movable:
        await callback.message.answer(
            "💰 <b>Withdraw ALL</b>\n\nNo withdrawable funds found "
            f"(everything below {SWEEP_DUST} USDT).",
            parse_mode="HTML", reply_markup=admin_menu(_chain_of(uid)))
        return
    tot_tron = sum(it[6] for it in movable if it[2] == "tron")
    tot_bsc = sum(it[6] for it in movable if it[2] == "bsc")
    n_tron = sum(1 for it in movable if it[2] == "tron")
    n_bsc = sum(1 for it in movable if it[2] == "bsc")
    hot_tron = tron_pay.get_hot_wallet_address()
    hot_bsc = bsc_pay.get_hot_wallet_address() if BSC_ENABLED else ""
    user_state[uid] = {"step": "withdraw_all", "items": movable}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes, withdraw ALL", callback_data="withdraw_all_confirm_yes")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="withdraw_all_confirm_no")],
    ])
    await callback.message.answer(
        f"💰 <b>Withdraw ALL → My Wallet</b>\n\n"
        f"🔴 TRON: <b>{tot_tron:.2f} USDT</b> ({n_tron} addresses)\n"
        f"💛 BSC: <b>{tot_bsc:.2f} USDT</b> ({n_bsc} addresses)\n"
        f"Skipped dust (<{SWEEP_DUST} USDT): {len(dust)}\n\n"
        f"To:\n<code>{hot_tron}</code>\n" +
        (f"<code>{hot_bsc}</code>\n\n" if hot_bsc else "\n") +
        f"⛽ <b>Bot ki koi fee nahi.</b> Sirf network gas lagegi — "
        f"TRON pe har address ~13 TRX, BSC pe ~$0.20. "
        f"Gas hot wallet se auto-fund hogi, bachi hui wapas aa jayegi.\n\n"
        f"⏳ Ye kuch minute le sakta hai. Confirm karoon?",
        parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("withdraw_all_confirm_"))
async def cb_withdraw_all_confirm(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    st = user_state.get(uid, {})
    if callback.data == "withdraw_all_confirm_no" or st.get("step") != "withdraw_all":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu(_chain_of(uid)))
        return
    items = st.get("items", [])
    user_state.pop(uid, None)
    if not items:
        await callback.message.answer("Nothing to withdraw.")
        return
    prog = await callback.message.answer(
        f"⏳ <b>Withdrawing from {len(items)} addresses...</b>\nIs me kuch minute lag sakte hain.",
        parse_mode="HTML")
    loop = asyncio.get_event_loop()

    def _run():
        out = []
        hot_tron = tron_pay.get_hot_wallet_address()
        hot_bsc = bsc_pay.get_hot_wallet_address() if BSC_ENABLED else ""
        for kind, label, chain, addr, key_kind, key_index, bal in items:
            amt = round(bal - 0.01, 2)
            if amt <= 0:
                out.append((label, chain, 0, "skip"))
                continue
            try:
                if chain == "bsc":
                    dest = hot_bsc
                    if key_index is None:
                        # already on hot wallet (v1-style) — nothing to move
                        out.append((label, chain, amt, "already_home"))
                        continue
                    pk_hex = bsc_pay.get_user_private_key_hex(key_index)
                    r = bsc_pay.send_usdt_from_with_gas(pk_hex, addr, dest, amt)
                else:
                    dest = hot_tron
                    if key_index is None:
                        out.append((label, chain, amt, "already_home"))
                        continue
                    pk = tron_pay.get_user_private_key(key_index)
                    r = tron_pay.send_usdt_from_with_gas(pk, dest, amt)
                if r.get("ok"):
                    out.append((label, chain, amt, "ok"))
                else:
                    out.append((label, chain, amt, f"fail: {r.get('error', '?')}"))
            except Exception as e:
                out.append((label, chain, amt, f"fail: {e}"))
        return out

    results = await loop.run_in_executor(None, _run)
    ok = [r for r in results if r[3] == "ok"]
    failed = [r for r in results if r[3] not in ("ok", "already_home", "skip")]
    home = [r for r in results if r[3] == "already_home"]
    tot = sum(r[2] for r in ok)
    lines = [f"💰 <b>Withdraw ALL — Done!</b>\n",
             f"Moved: <b>{tot:.2f} USDT</b> ({len(ok)} addresses)"]
    if home:
        lines.append(f"Already in wallet: {len(home)}")
    for label, chain, amt, err in failed:
        lines.append(f"❌ {label} ({CHAIN_LABEL[chain]}): {amt:.2f} — {err}")
    if failed:
        lines.append("\n<i>Failed wale dobara try karo ya alag se withdraw karo.</i>")
    try:
        await prog.delete()
    except Exception:
        pass
    await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                  reply_markup=admin_menu(_chain_of(uid)))


@dp.callback_query(F.data == "a_swap")
async def cb_a_swap(callback: types.CallbackQuery):
    """Start USDT->TRX swap (admin only)."""
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    user_state[callback.from_user.id] = {"step": "swap_amount"}
    await callback.message.answer(
        "🔄 <b>Swap USDT → TRX</b>\n\n"
        "How much USDT to swap? (e.g., 10)\n"
        "<i>Uses SunSwap. ~5 TRX fee for the swap itself.</i>",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("swap_confirm_"))
async def cb_swap_confirm(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    st = user_state.get(uid, {})
    chain = _chain_of(uid)
    if callback.data == "swap_confirm_no":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu(chain))
        return
    await callback.message.answer("⏳ Swapping via SunSwap...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, tron_pay.swap_usdt_to_trx, st.get("swap_amt", 0))
    user_state.pop(uid, None)
    if result.get("ok"):
        await callback.message.answer(
            f"✅ <b>Swapped!</b>\n\nReceived: <b>{result.get('trx_received', 0):.2f} TRX</b>\n"
            f"TxID: <code>{result.get('txid', '')}</code>",
            parse_mode="HTML", reply_markup=admin_menu(chain))
    else:
        await callback.message.answer(
            f"❌ Swap failed: {result.get('error', '?')}",
            parse_mode="HTML", reply_markup=admin_menu(chain))


@dp.callback_query(F.data == "a_backup")
async def cb_a_backup(callback: types.CallbackQuery):
    """Send deal_states.json backup to admin via Telegram."""
    await callback.answer("Preparing...")
    if not _is_admin(callback.from_user.id):
        return
    import os
    from aiogram.types import FSInputFile
    path = "/root/deal_states.json"
    if not os.path.exists(path):
        await callback.message.answer("❌ No deal data yet.")
        return
    try:
        await callback.message.answer_document(
            FSInputFile(path, filename=f"deal_backup_{callback.message.date.strftime('%Y%m%d')}.json"),
            caption="📦 <b>Deal Backup</b>\n\nIs file ko <b>save</b> kar lo!\nVPS kharab ho jaye to isi se paisa wapas milega.",
            parse_mode="HTML",
        )
    except Exception as e:
        await callback.message.answer(f"❌ Backup failed: {e}")


@dp.callback_query(F.data == "w_send")
async def cb_send(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        await callback.message.answer("⛔ Admin only.")
        return
    chain = _chain_of(callback.from_user.id)
    user_state[callback.from_user.id] = {"step": "address", "chain": chain}
    await callback.message.answer(
        f"💸 <b>Send USDT</b> (Admin) — {CHAIN_LABEL[chain]}\n\n"
        f"Send the destination {_dest_kind_label(chain)} address:",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("send_confirm_"))
async def cb_send_confirm(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    st = user_state.get(uid, {})
    chain = st.get("chain", "tron")
    if callback.data == "send_confirm_no":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu(chain))
        return
    # Yes — send via hot wallet
    await callback.message.answer("⏳ Broadcasting...")
    loop = asyncio.get_event_loop()
    def _send():
        if chain == "bsc":
            return bsc_pay.send_usdt(st["address"], st["amount"])
        return tron_pay.send_usdt(st["address"], st["amount"])
    result = await loop.run_in_executor(None, _send)
    user_state.pop(uid, None)
    if result.get("ok"):
        txid = result.get("txid", "")
        await callback.message.answer(
            f"✅ <b>Sent!</b> ({CHAIN_LABEL[chain]})\n\nTxID: <code>{txid}</code>\n"
            f"{EXPLORER_TX[chain]}{txid}",
            parse_mode="HTML", reply_markup=admin_menu(chain))
    else:
        await callback.message.answer(
            f"❌ Failed: {result.get('error', '?')}",
            parse_mode="HTML", reply_markup=admin_menu(chain))


@dp.message(F.text)
async def handle_text(message: types.Message):
    uid = message.from_user.id
    st = user_state.get(uid)
    if not st:
        return
    if not _is_admin(uid):
        user_state.pop(uid, None)
        return
    text = message.text.strip()
    loop = asyncio.get_event_loop()
    if st["step"] == "swap_amount":
        try:
            amt = float(text)
            if amt <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Invalid amount. Try again:")
            return
        st["swap_amt"] = amt
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm Swap", callback_data="swap_confirm_yes")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="swap_confirm_no")],
        ])
        await message.answer(
            f"Confirm swap:\n\n<b>{amt} USDT</b> → TRX\n<i>Via SunSwap V2</i>",
            parse_mode="HTML", reply_markup=kb)
        return
    if st["step"] == "user_withdraw":
        # Step 1: get destination address for user withdraw
        chain = st.get("chain", "tron")
        if not _valid_dest(text, chain):
            await message.answer(f"❌ Invalid {_dest_kind_label(chain)} address. Try again:")
            return
        st["address"] = text
        st["step"] = "user_withdraw_amount"
        await message.answer("💰 Send the amount in USDT (or 'max' for full balance):")
        return
    if st["step"] == "user_withdraw_amount":
        chain = st.get("chain", "tron")
        users = load_users()
        rec = users.get(st.get("target_uid", ""), {})
        user_addr = _addr_for(rec, chain)
        user_index = rec.get("index")
        if user_index is None or not user_addr:
            await message.answer("❌ User address not found.")
            user_state.pop(uid, None)
            return
        if text.lower() == "max":
            if chain == "bsc":
                bal = await loop.run_in_executor(None, bsc_pay.get_usdt_balance, user_addr)
            else:
                bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, user_addr)
            amt = round(bal - 0.01, 2) if bal > 0.01 else 0
        else:
            try:
                amt = float(text)
                if amt <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Invalid amount. Try again (or 'max'):")
                return
        st["amount"] = amt
        st["user_addr"] = user_addr
        st["user_index"] = user_index
        un = f"@{rec['username']}" if rec.get("username") else rec.get("first_name", "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm", callback_data="user_wd_confirm_yes")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="user_wd_confirm_no")],
        ])
        await message.answer(
            f"Confirm withdraw ({CHAIN_LABEL[chain]}):\n\n<b>{amt} USDT</b>\nFrom: {un}\n<code>{user_addr}</code>\nTo: <code>{st['address']}</code>\n\n"
            f"<i>⛽ Gas ({GAS_LABEL[chain]}) auto-funded from your hot wallet, leftover swept back.</i>",
            parse_mode="HTML", reply_markup=kb)
        return
    if st["step"] == "deal_withdraw":
        # Step 1: get destination address for deal withdraw
        chain = st.get("chain", "tron")
        if not _valid_dest(text, chain):
            await message.answer(f"❌ Invalid {_dest_kind_label(chain)} address. Try again:")
            return
        st["address"] = text.strip()
        st["step"] = "deal_withdraw_amount"
        await message.answer("💰 Send the amount in USDT (or 'max' for full balance):")
        return
    if st["step"] == "deal_withdraw_amount":
        import json
        chain = st.get("chain", "tron")
        loop = asyncio.get_event_loop()
        def _load():
            try:
                with open("/root/deal_states.json") as f:
                    return json.load(f)
            except Exception:
                return {}
        states = await loop.run_in_executor(None, _load)
        deal = states.get(st["deal_chat_id"], {})
        if chain == "bsc":
            deal_addr = deal.get("bsc_deposit_address", "")
            hd_index = deal.get("hd_index")
            get_bal = bsc_pay.get_usdt_balance
        else:
            deal_addr = deal.get("tron_deposit_address", "")
            hd_index = deal.get("hd_index", deal.get("tron_hd_index"))
            get_bal = tron_pay.get_usdt_balance
        if text.lower() == "max":
            bal = await loop.run_in_executor(None, get_bal, deal_addr)
            # Leave a tiny bit for safety, withdraw almost all
            amt = round(bal - 0.01, 2) if bal > 0.01 else 0
        else:
            try:
                amt = float(text)
                if amt <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Invalid amount. Try again (or 'max'):")
                return
        st["amount"] = amt
        st["deal_addr"] = deal_addr
        st["hd_index"] = hd_index
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm", callback_data="deal_wd_confirm_yes")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="deal_wd_confirm_no")],
        ])
        await message.answer(
            f"Confirm withdraw from deal:\n\n<b>{amt} USDT</b>\nFrom: <code>{deal_addr}</code>\nTo: <code>{st['address']}</code>",
            parse_mode="HTML", reply_markup=kb)
        return
    if st["step"] == "address":
        chain = st.get("chain", "tron")
        if not _valid_dest(text, chain):
            await message.answer(f"❌ Invalid {_dest_kind_label(chain)} address. Try again:")
            return
        st["address"] = text
        st["step"] = "amount"
        await message.answer("💰 Send the amount in USDT:")
    elif st["step"] == "amount":
        try:
            amt = float(text)
            if amt <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Invalid amount. Try again:")
            return
        st["amount"] = amt
        chain = st.get("chain", "tron")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm", callback_data="send_confirm_yes")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="send_confirm_no")],
        ])
        await message.answer(
            f"Confirm send ({CHAIN_LABEL[chain]}):\n\n<b>{amt} USDT</b>\nTo: <code>{st['address']}</code>",
            parse_mode="HTML", reply_markup=kb)


async def deposit_monitor():
    """Background: poll all user addresses, notify admin on new deposits."""
    import requests
    await asyncio.sleep(10)  # let bot start first
    logger.info("Deposit monitor started")
    while True:
        try:
            users = load_users()
            for uid, rec in users.items():
                addr = rec.get("address", "")
                if not addr:
                    continue
                seen = set(rec.get("seen_txids", []))
                try:
                    r = tron_pay._get_with_retry(
                        f"https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20",
                        params={"limit": 10, "only_confirmed": "true",
                                "contract_address": tron_pay.USDT_CONTRACT},
                    )
                    if r.status_code != 200:
                        continue
                    for t in r.json().get("data", []):
                        txid = t.get("transaction_id", "")
                        if txid in seen or t.get("to", "") != addr:
                            continue
                        amt = int(t.get("value", 0)) / 1_000_000
                        seen.add(txid)
                        # Notify admin
                        un = f"@{rec['username']}" if rec.get("username") else ""
                        name = rec.get("first_name", "")
                        for aid in ADMIN_IDS:
                            try:
                                await bot.send_message(
                                    aid,
                                    f"💰 <b>Deposit Received!</b>\n\n"
                                    f"User: {un} ({name})\n"
                                    f"Amount: <b>{amt:.2f} USDT</b>\n"
                                    f"Address: <code>{addr}</code>\n"
                                    f"TxID: <code>{txid[:16]}...</code>",
                                    parse_mode="HTML",
                                )
                            except Exception as e:
                                logger.warning(f"Admin notify failed: {e}")
                        # Notify the user too
                        try:
                            await bot.send_message(
                                int(uid),
                                f"✅ <b>Deposit confirmed!</b>\n\n"
                                f"<b>{amt:.2f} USDT</b> received.",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                    # Persist seen txids (keep last 50)
                    rec["seen_txids"] = list(seen)[-50:]
                except Exception as e:
                    logger.warning(f"Monitor poll error for {addr}: {e}")
                # --- BSC deposits (BEP20) ---
                if BSC_ENABLED:
                    try:
                        bsc_addr = rec.get("bsc_address") or ""
                        if bsc_addr:
                            if "bsc_last_block" not in rec:
                                # First run: start watching from now (no backfill spam)
                                _, latest = bsc_pay.get_new_deposits(bsc_addr, 10 ** 12)
                                rec["bsc_last_block"] = latest
                            else:
                                deposits, latest = bsc_pay.get_new_deposits(
                                    bsc_addr, rec["bsc_last_block"])
                                rec["bsc_last_block"] = latest
                                seen_bsc = set(rec.get("seen_bsc_txids", []))
                                for amt, txid in deposits:
                                    if txid in seen_bsc:
                                        continue
                                    seen_bsc.add(txid)
                                    un = f"@{rec['username']}" if rec.get("username") else ""
                                    name = rec.get("first_name", "")
                                    for aid in ADMIN_IDS:
                                        try:
                                            await bot.send_message(
                                                aid,
                                                f"💰 <b>Deposit Received!</b> (BNB Smart Chain)\n\n"
                                                f"User: {un} ({name})\n"
                                                f"Amount: <b>{amt:.2f} USDT</b>\n"
                                                f"Address: <code>{bsc_addr}</code>\n"
                                                f"TxID: <code>{txid[:16]}...</code>",
                                                parse_mode="HTML",
                                            )
                                        except Exception as e:
                                            logger.warning(f"Admin notify failed: {e}")
                                    try:
                                        await bot.send_message(
                                            int(uid),
                                            f"✅ <b>Deposit confirmed!</b> (BNB Smart Chain)\n\n"
                                            f"<b>{amt:.2f} USDT</b> received.",
                                            parse_mode="HTML",
                                        )
                                    except Exception:
                                        pass
                                rec["seen_bsc_txids"] = list(seen_bsc)[-50:]
                    except Exception as e:
                        logger.warning(f"BSC monitor poll error: {e}")
            save_users(users)
        except Exception as e:
            logger.error(f"Deposit monitor error: {e}")
        await asyncio.sleep(30)


async def main():
    if not (USE_TRON and TRON_AVAILABLE):
        logger.error("TRON not configured! Set WALLET_USE_TRON=true, TRON_PRIVATE_KEY, WALLET_SEED_PHRASE")
        sys.exit(1)
    asyncio.create_task(deposit_monitor())
    logger.info(f"Wallet bot starting (multi-user, TRON default"
                f"{', BSC enabled' if BSC_ENABLED else ', BSC disabled'})")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
