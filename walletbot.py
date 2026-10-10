#!/usr/bin/env python3
"""
Personal Wallet Bot — Multi-user TRON USDT (TRC20).
- Each user gets a unique deposit address (BIP44 HD).
- Admin notified on every deposit (username + name + amount).
- Withdraw/payout: admin only.
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

if not BOT_TOKEN:
    logger.error("WALLET_BOT_TOKEN not set!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

USERS_FILE = "/root/wallet_users.json"
user_state: dict[int, dict] = {}


def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


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
        users[uid] = {
            "index": idx,
            "address": address,
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
    return users[uid]


def user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 My Deposit Address", callback_data="u_address")],
        [InlineKeyboardButton(text="💰 My Balance", callback_data="u_balance")],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 My Deposit Address", callback_data="u_address")],
        [InlineKeyboardButton(text="💰 My Balance", callback_data="u_balance")],
        [InlineKeyboardButton(text="💸 Send (Admin)", callback_data="w_send")],
        [InlineKeyboardButton(text="👥 Users", callback_data="a_users")],
        [InlineKeyboardButton(text="🤝 Deal Funds", callback_data="a_deals")],
        [InlineKeyboardButton(text="📦 Backup Deals", callback_data="a_backup")],
    ])


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not (USE_TRON and TRON_AVAILABLE):
        await message.answer("⛔ Wallet not configured.")
        return
    u = message.from_user
    rec = await asyncio.get_event_loop().run_in_executor(
        None, get_or_create_user, u.id, u.username, u.first_name)
    menu = admin_menu() if _is_admin(u.id) else user_menu()
    role = " (Admin)" if _is_admin(u.id) else ""
    await message.answer(
        f"👛 <b>My Wallet</b>{role}\n\n"
        f"Network: <b>TRON</b> | Token: <b>USDT (TRC20)</b>\n\n"
        f"Your deposit address:\n<code>{rec['address']}</code>\n\n"
        f"Choose an action:",
        parse_mode="HTML",
        reply_markup=menu,
    )


@dp.callback_query(F.data == "u_address")
async def cb_u_address(callback: types.CallbackQuery):
    await callback.answer()
    u = callback.from_user
    rec = await asyncio.get_event_loop().run_in_executor(
        None, get_or_create_user, u.id, u.username, u.first_name)
    await callback.message.answer(
        f"📥 <b>Your USDT (TRC20) Deposit Address</b>\n\n"
        f"<code>{rec['address']}</code>\n\n"
        f"Send USDT here. Admin is notified automatically on deposit!",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "u_balance")
async def cb_u_balance(callback: types.CallbackQuery):
    await callback.answer("Checking...")
    u = callback.from_user
    rec = await asyncio.get_event_loop().run_in_executor(
        None, get_or_create_user, u.id, u.username, u.first_name)
    loop = asyncio.get_event_loop()
    bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, rec["address"])
    # Admin sees total across all funds
    if _is_admin(u.id):
        import json
        def _totals():
            total = bal
            # Deal escrow funds
            try:
                with open("/root/deal_states.json") as f:
                    states = json.load(f)
                for s in states.values():
                    addr = s.get("tron_deposit_address")
                    if addr and addr != rec["address"]:
                        total += tron_pay.get_usdt_balance(addr)
            except Exception:
                pass
            # User deposits
            try:
                users = load_users()
                for uid, urec in users.items():
                    a = urec.get("address", "")
                    if a and a != rec["address"]:
                        total += tron_pay.get_usdt_balance(a)
            except Exception:
                pass
            return total
        total = await loop.run_in_executor(None, _totals)
        await callback.message.answer(
            f"💰 <b>Total Balance</b>\n\n"
            f"My Wallet: <b>{bal:.2f}</b> USDT\n"
            f"All Funds: <b>{total:.2f}</b> USDT\n\n"
            f"<i>Includes deal escrow + user deposits</i>",
            parse_mode="HTML",
        )
        return
    await callback.message.answer(
        f"💰 <b>Your Balance</b>\n\nUSDT: <b>{bal:.2f}</b>",
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
    lines = ["👥 <b>Wallet Users</b>\n"]
    for uid, rec in list(users.items())[:20]:
        bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, rec["address"])
        un = f"@{rec['username']}" if rec.get("username") else rec.get("first_name", uid)
        lines.append(f"• {un} — <b>{bal:.2f}</b> USDT\n  <code>{rec['address']}</code>")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")


@dp.callback_query(F.data == "a_deals")
async def cb_a_deals(callback: types.CallbackQuery):
    """Show all deal escrow addresses with balances (admin only)."""
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
    deals = [(cid, s) for cid, s in states.items() if s.get("tron_deposit_address")]
    if not deals:
        await callback.message.answer("🤝 No deal escrow addresses yet.")
        return
    lines = ["🤝 <b>Deal Escrow Funds</b>\n"]
    kb_rows = []
    for cid, s in deals[-10:]:  # last 10
        addr = s["tron_deposit_address"]
        bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, addr)
        stage = s.get("stage", "?")
        lines.append(f"• Chat {cid} — <b>{bal:.2f}</b> USDT ({stage})\n  <code>{addr}</code>")
        if bal > 0.01:
            kb_rows.append([InlineKeyboardButton(
                text=f"💸 Withdraw {bal:.2f} from {cid}",
                callback_data=f"deal_wd_{cid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("deal_wd_") & ~F.data.startswith("deal_wd_confirm_"))
async def cb_deal_withdraw(callback: types.CallbackQuery):
    """Start manual withdraw from a deal address (admin only)."""
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    chat_id = callback.data[len("deal_wd_"):]
    user_state[callback.from_user.id] = {"step": "deal_withdraw", "deal_chat_id": chat_id}
    await callback.message.answer(
        f"💸 <b>Withdraw from Deal {chat_id}</b>\n\nSend the destination TRON address:",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("deal_wd_confirm_"))
async def cb_deal_wd_confirm(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    st = user_state.get(uid, {})
    if callback.data == "deal_wd_confirm_no":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        return
    await callback.message.answer("⏳ Broadcasting...")
    loop = asyncio.get_event_loop()
    def _do():
        pk = tron_pay.get_user_private_key(st["hd_index"])
        return tron_pay.send_usdt_from_with_gas(pk, st["address"], st["amount"])
    result = await loop.run_in_executor(None, _do)
    user_state.pop(uid, None)
    if result.get("ok"):
        txid = result.get("txid", "")
        await callback.message.answer(
            f"✅ <b>Withdrawn!</b>\n\nTxID: <code>{txid}</code>\n"
            f"https://tronscan.org/#/transaction/{txid}",
            parse_mode="HTML", reply_markup=admin_menu())
    else:
        await callback.message.answer(
            f"❌ Failed: {result.get('error', '?')}\n\n"
            f"Tip: deal address needs TRX for gas!",
            parse_mode="HTML", reply_markup=admin_menu())


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
    user_state[callback.from_user.id] = {"step": "address"}
    await callback.message.answer(
        "💸 <b>Send USDT</b> (Admin)\n\nSend the destination TRON address:",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("send_confirm_"))
async def cb_send_confirm(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    uid = callback.from_user.id
    st = user_state.get(uid, {})
    if callback.data == "send_confirm_no":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        return
    # Yes — send via hot wallet
    await callback.message.answer("⏳ Broadcasting...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: tron_pay.send_usdt(st["address"], st["amount"]))
    user_state.pop(uid, None)
    if result.get("ok"):
        txid = result.get("txid", "")
        await callback.message.answer(
            f"✅ <b>Sent!</b>\n\nTxID: <code>{txid}</code>\n"
            f"https://tronscan.org/#/transaction/{txid}",
            parse_mode="HTML", reply_markup=admin_menu())
    else:
        await callback.message.answer(
            f"❌ Failed: {result.get('error', '?')}",
            parse_mode="HTML", reply_markup=admin_menu())


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
    if st["step"] == "deal_withdraw":
        # Step 1: get destination address for deal withdraw
        if not (text.startswith("T") and len(text) == 34):
            await message.answer("❌ Invalid TRON address. Try again:")
            return
        st["address"] = text
        st["step"] = "deal_withdraw_amount"
        await message.answer("💰 Send the amount in USDT (or 'max' for full balance):")
        return
    if st["step"] == "deal_withdraw_amount":
        import json
        loop = asyncio.get_event_loop()
        def _load():
            try:
                with open("/root/deal_states.json") as f:
                    return json.load(f)
            except Exception:
                return {}
        states = await loop.run_in_executor(None, _load)
        deal = states.get(st["deal_chat_id"], {})
        deal_addr = deal.get("tron_deposit_address", "")
        hd_index = deal.get("tron_hd_index")
        if text.lower() == "max":
            bal = await loop.run_in_executor(None, tron_pay.get_usdt_balance, deal_addr)
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
        if not (text.startswith("T") and len(text) == 34):
            await message.answer("❌ Invalid TRON address. Try again:")
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
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm", callback_data="send_confirm_yes")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="send_confirm_no")],
        ])
        await message.answer(
            f"Confirm send:\n\n<b>{amt} USDT</b>\nTo: <code>{st['address']}</code>",
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
            save_users(users)
        except Exception as e:
            logger.error(f"Deposit monitor error: {e}")
        await asyncio.sleep(30)


async def main():
    if not (USE_TRON and TRON_AVAILABLE):
        logger.error("TRON not configured! Set WALLET_USE_TRON=true, TRON_PRIVATE_KEY, WALLET_SEED_PHRASE")
        sys.exit(1)
    asyncio.create_task(deposit_monitor())
    logger.info("Wallet bot starting (TRON multi-user)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
