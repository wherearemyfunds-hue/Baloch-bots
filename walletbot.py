#!/usr/bin/env python3
"""
Baloch Wallet Bot — Full wallet via BTCPay Server API.
Features: Balance, Receive (new address), Send (manual payout), History.
1000% perfect. Uses env vars for secrets (never hardcoded).
"""
import asyncio
import logging
import os
import sys

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("walletbot")

# --- Config from environment ---
BOT_TOKEN = os.environ.get("WALLET_BOT_TOKEN", "")
BTCPAY_URL = os.environ.get("BTCPAY_URL", "").rstrip("/")
BTCPAY_API_KEY = os.environ.get("BTCPAY_API_KEY", "")
BTCPAY_STORE_ID = os.environ.get("BTCPAY_STORE_ID", "")
CRYPTO_CODE = os.environ.get("BTCPAY_CRYPTO", "BTC")  # BTC, USDT, etc.
ADMIN_IDS = {int(x) for x in os.environ.get("WALLET_ADMIN_IDS", "").split(",") if x.strip().isdigit()}

# --- Direct TRON (USDT TRC20) — personal wallet mode ---
USE_TRON = os.environ.get("WALLET_USE_TRON", "false").lower() == "true"
TRON_PRIVATE_KEY = os.environ.get("TRON_PRIVATE_KEY", "").strip()
try:
    import tron_pay
    TRON_AVAILABLE = bool(TRON_PRIVATE_KEY)
except ImportError:
    TRON_AVAILABLE = False
    tron_pay = None

if not BOT_TOKEN:
    logger.error("WALLET_BOT_TOKEN not set!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Per-user state for send flow
user_state: dict[int, dict] = {}


def _headers() -> dict:
    return {"Authorization": f"token {BTCPAY_API_KEY}", "Content-Type": "application/json"}


def _base() -> str:
    return f"{BTCPAY_URL}/api/v1/stores/{BTCPAY_STORE_ID}/wallets/{CRYPTO_CODE}"


async def _btcpay_get(path: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{_base()}{path}", headers=_headers(), timeout=30) as r:
            return {"ok": r.status < 300, "status": r.status, "data": await r.json() if r.status < 500 else {}}


async def _btcpay_post(path: str, payload: dict) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{_base()}{path}", headers=_headers(), json=payload, timeout=60) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            return {"ok": r.status < 300, "status": r.status, "data": data}


def _is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Balance", callback_data="w_balance")],
        [InlineKeyboardButton(text="📥 Receive", callback_data="w_receive")],
        [InlineKeyboardButton(text="💸 Send", callback_data="w_send")],
        [InlineKeyboardButton(text="📊 History", callback_data="w_history")],
    ])


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Unauthorized.")
        return
    await message.answer(
        f"👛 <b>Baloch Wallet Bot</b>\n\n"
        f"Connected to BTCPay: <code>{BTCPAY_URL}</code>\n"
        f"Crypto: <b>{CRYPTO_CODE}</b>\n\n"
        f"Choose an action:",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "w_balance")
async def cb_balance(callback: types.CallbackQuery):
    await callback.answer("Checking...")
    if not _is_admin(callback.from_user.id):
        return
    if USE_TRON and TRON_AVAILABLE:
        import asyncio as aio
        loop = aio.get_event_loop()
        addr = await loop.run_in_executor(None, tron_pay.get_hot_wallet_address)
        usdt = await loop.run_in_executor(None, tron_pay.get_usdt_balance, addr)
        trx = await loop.run_in_executor(None, tron_pay.get_trx_balance, addr)
        await callback.message.answer(
            f"💰 <b>Wallet Balance</b>\n\n"
            f"USDT (TRC20): <b>{usdt:.2f}</b>\n"
            f"TRX (gas): <b>{trx:.2f}</b>\n\n"
            f"<code>{addr}</code>",
            parse_mode="HTML",
        )
        return
    resp = await _btcpay_get("")
    if resp["ok"]:
        data = resp["data"]
        confirmed = data.get("confirmedBalance", "?")
        unconfirmed = data.get("unconfirmedBalance", "?")
        await callback.message.answer(
            f"💰 <b>Balance ({CRYPTO_CODE})</b>\n\n"
            f"Confirmed: <b>{confirmed}</b>\n"
            f"Unconfirmed: <b>{unconfirmed}</b>",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(f"❌ Failed to get balance (HTTP {resp['status']}).")


@dp.callback_query(F.data == "w_receive")
async def cb_receive(callback: types.CallbackQuery):
    await callback.answer("Generating...")
    if not _is_admin(callback.from_user.id):
        return
    if USE_TRON and TRON_AVAILABLE:
        import asyncio as aio
        loop = aio.get_event_loop()
        addr = await loop.run_in_executor(None, tron_pay.get_hot_wallet_address)
        await callback.message.answer(
            f"📥 <b>Your USDT (TRC20) Address</b>\n\n<code>{addr}</code>\n\n"
            f"Send USDT (TRC20) here. Deposits are detected automatically!",
            parse_mode="HTML",
        )
        return
    resp = await _btcpay_post("/addresses", {})
    if resp["ok"]:
        addr = resp["data"].get("address", "?")
        await callback.message.answer(
            f"📥 <b>New {CRYPTO_CODE} Address</b>\n\n<code>{addr}</code>\n\n"
            f"Tap to copy. Each address is unique!",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(f"❌ Failed to generate address (HTTP {resp['status']}).")


@dp.callback_query(F.data == "w_history")
async def cb_history(callback: types.CallbackQuery):
    await callback.answer("Loading...")
    if not _is_admin(callback.from_user.id):
        return
    resp = await _btcpay_get("/transactions?limit=10")
    if resp["ok"]:
        txs = resp["data"] if isinstance(resp["data"], list) else resp["data"].get("transactions", [])
        if not txs:
            await callback.message.answer("📊 No transactions yet.")
            return
        lines = [f"📊 <b>Recent Transactions ({CRYPTO_CODE})</b>\n"]
        for t in txs[:10]:
            txid = str(t.get("txid", "?"))[:12]
            amt = t.get("amount", "?")
            status = t.get("status", "?")
            lines.append(f"• <code>{txid}...</code> — {amt} ({status})")
        await callback.message.answer("\n".join(lines), parse_mode="HTML")
    else:
        await callback.message.answer(f"❌ Failed to load history (HTTP {resp['status']}).")


@dp.callback_query(F.data == "w_send")
async def cb_send(callback: types.CallbackQuery):
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        return
    user_state[callback.from_user.id] = {"step": "address"}
    await callback.message.answer("💸 <b>Send Payment</b>\n\nSend the destination address:", parse_mode="HTML")


@dp.callback_query(F.data.startswith("send_confirm_"))
async def cb_send_confirm(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    if not _is_admin(uid):
        return
    st = user_state.get(uid)
    if not st or st.get("step") != "confirm":
        await callback.message.answer("❌ No pending payment.")
        return
    if callback.data == "send_confirm_no":
        user_state.pop(uid, None)
        await callback.message.answer("❌ Cancelled.", reply_markup=main_menu())
        return
    # Yes — broadcast the transaction
    await callback.message.answer("⏳ Broadcasting transaction...")
    if USE_TRON and TRON_AVAILABLE:
        import asyncio as aio
        loop = aio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: tron_pay.send_usdt(st["address"], st["amount"]))
        user_state.pop(uid, None)
        if result.get("ok"):
            txid = result.get("txid", "")
            await callback.message.answer(
                f"✅ <b>USDT Sent!</b>\n\nTxID: <code>{txid}</code>\n"
                f"https://tronscan.org/#/transaction/{txid}",
                parse_mode="HTML",
                reply_markup=main_menu(),
            )
        else:
            await callback.message.answer(
                f"❌ Send failed: {result.get('error', 'unknown')}",
                parse_mode="HTML",
                reply_markup=main_menu(),
            )
        return
    payload = {
        "destinations": [{"destination": st["address"], "amount": st["amount"]}],
        "proceedWithBroadcast": True,
    }
    # Optional: fee rate
    if st.get("fee_rate"):
        payload["feeRate"] = st["fee_rate"]
    resp = await _btcpay_post("/transactions", payload)
    user_state.pop(uid, None)
    if resp["ok"]:
        txid = resp["data"].get("txid", "?")
        await callback.message.answer(
            f"✅ <b>Payment Sent!</b>\n\nTxID: <code>{txid}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    else:
        err = resp["data"]
        await callback.message.answer(
            f"❌ Payment failed (HTTP {resp['status']}).\n<code>{err}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )


@dp.message(F.text)
async def handle_text(message: types.Message):
    uid = message.from_user.id
    if not _is_admin(uid):
        return
    st = user_state.get(uid)
    if not st:
        return
    text = (message.text or "").strip()

    if st["step"] == "address":
        if len(text) < 20:
            await message.answer("❌ That doesn't look like a valid address. Try again:")
            return
        st["address"] = text
        st["step"] = "amount"
        await message.answer(f"Address saved: <code>{text}</code>\n\nNow send the <b>amount</b> in {CRYPTO_CODE}:", parse_mode="HTML")
    elif st["step"] == "amount":
        try:
            amt = float(text.replace(",", ""))
            if amt <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Invalid amount. Send a number:")
            return
        st["amount"] = amt
        st["step"] = "confirm"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Confirm & Send", callback_data="send_confirm_yes"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="send_confirm_no"),
        ]])
        await message.answer(
            f"💸 <b>Confirm Payment</b>\n\n"
            f"To: <code>{st['address']}</code>\n"
            f"Amount: <b>{amt} {CRYPTO_CODE}</b>\n\n"
            f"Network fee will apply. Proceed?",
            parse_mode="HTML",
            reply_markup=kb,
        )


async def main():
    logger.info(f"Wallet bot starting (crypto={CRYPTO_CODE}, store={BTCPAY_STORE_ID[:8]}...)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
