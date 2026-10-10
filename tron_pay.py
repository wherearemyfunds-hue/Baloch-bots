#!/usr/bin/env python3
"""
Direct TRON USDT (TRC20) payments — no middleman, no platform fee.
Uses TronGrid public API + tronpy for signing.
Only TRON gas fee applies (~$0.30-2.00 per tx).
"""
import logging
import os
import time

import requests
from tronpy import Tron
from tronpy.keys import PrivateKey

logger = logging.getLogger("tron_pay")

# USDT TRC20 contract (mainnet)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
# SunSwap V2 Router (mainnet) - for USDT<->TRX swaps
SUNSWAP_V2_ROUTER = "TKzxdSv2FZKQrEqkKVgp5DcwEXBEKMg2Ax"
# Wrapped TRX (for swap paths)
WTRX_CONTRACT = "TNUC9Qb1rRpS5CbWLmNMxXBjyFoydXjWFR"
TRONGRID_API = "https://api.trongrid.io"
# USDT has 6 decimals
USDT_DECIMALS = 6

_client = None
_master_key = None


def _get_client() -> Tron:
    global _client
    if _client is None:
        from tronpy.providers import HTTPProvider
        api_key = os.environ.get("TRONGRID_API_KEY", "").strip()
        if api_key:
            # Use API key for higher rate limits (free at trongrid.io)
            provider = HTTPProvider(
                endpoint_uri="https://api.trongrid.io",
                api_key=api_key,
            )
            _client = Tron(provider=provider, network="mainnet")
        else:
            _client = Tron(network="mainnet")
    return _client


def _trongrid_headers() -> dict:
    """Headers for direct TronGrid REST calls (with API key if set)."""
    h = {"Content-Type": "application/json"}
    api_key = os.environ.get("TRONGRID_API_KEY", "").strip()
    if api_key:
        h["TRON-PRO-API-KEY"] = api_key
    return h


def _get_with_retry(url: str, params: dict = None, max_retries: int = 5) -> requests.Response:
    """GET with exponential backoff on 429 rate limits."""
    for attempt in range(max_retries):
        r = requests.get(url, params=params, headers=_trongrid_headers())
        if r.status_code != 429:
            return r
        wait = (2 ** attempt) + 1  # 2, 3, 5, 9, 17 seconds
        logger.warning(f"TronGrid 429 rate limit, retrying in {wait}s (attempt {attempt+1}/{max_retries})")
        time.sleep(wait)
    return r  # return last response even if 429


def _get_master_key() -> PrivateKey:
    """Load the hot wallet private key from env (never hardcoded)."""
    global _master_key
    if _master_key is None:
        hex_key = os.environ.get("TRON_PRIVATE_KEY", "").strip()
        if not hex_key:
            raise RuntimeError("TRON_PRIVATE_KEY not set in environment")
        # Support 0x prefix or raw hex
        if hex_key.startswith("0x"):
            hex_key = hex_key[2:]
        _master_key = PrivateKey(bytes.fromhex(hex_key))
    return _master_key


# --- HD Wallet (BIP44) for per-user deposit addresses ---
# Path: m/44'/195'/0'/0/{index}  (195 = TRON coin type)
_hd_ctx = None


def _get_hd_ctx():
    """Get BIP44 HD context from seed phrase."""
    global _hd_ctx
    if _hd_ctx is None:
        from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
        seed_phrase = os.environ.get("WALLET_SEED_PHRASE", "").strip()
        if not seed_phrase:
            raise RuntimeError("WALLET_SEED_PHRASE not set — needed for per-user addresses")
        seed = Bip39SeedGenerator(seed_phrase).Generate()
        _hd_ctx = Bip44.FromSeed(seed, Bip44Coins.TRON).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT)
    return _hd_ctx


def get_user_deposit_address(user_index: int) -> str:
    """
    Derive a unique TRON deposit address for a user.
    Path: m/44'/195'/0'/0/{user_index}
    Same seed phrase recovers all addresses in any BIP44 TRON wallet.
    """
    ctx = _get_hd_ctx()
    addr = ctx.AddressIndex(user_index).PublicKey().ToAddress()
    return addr


def get_user_private_key(user_index: int) -> PrivateKey:
    """Get the private key for a user's derived address (for sweeping)."""
    ctx = _get_hd_ctx()
    raw_hex = ctx.AddressIndex(user_index).PrivateKey().Raw().ToHex()
    return PrivateKey(bytes.fromhex(raw_hex))


def get_hot_wallet_address() -> str:
    """Return the main hot wallet TRON address."""
    return _get_master_key().public_key.to_base58check_address()


def generate_deposit_address(deal_id: str) -> str:
    """
    Generate a unique deposit address per deal.
    Uses HD derivation from master key: m/44'/195'/0'/0/{index}
    where index is derived from deal_id hash for determinism.
    For simplicity v1: derive from deal_id via tronpy HD wallet if seed available,
    else fall back to hot wallet address with deal memo tracking.

    NOTE: tronpy PrivateKey from raw hex doesn't support HD derivation directly.
    v1 uses the hot wallet address for all deposits and tracks by amount+deal_id.
    v2 can upgrade to xpub-based unique addresses.
    """
    # v1: single hot wallet address, deposits tracked by expected amount
    # This is simple and reliable. Unique addresses come in v2.
    return get_hot_wallet_address()


def get_usdt_balance(address: str) -> float:
    """Get USDT (TRC20) balance via contract balanceOf (most reliable)."""
    try:
        client = _get_client()
        contract = client.get_contract(USDT_CONTRACT)
        # Call balanceOf directly on the contract
        result = contract.functions.balanceOf(address)
        # tronpy returns int for uint256
        raw = int(result)
        return raw / (10 ** USDT_DECIMALS)
    except Exception as e:
        logger.warning(f"Contract balanceOf failed, trying REST: {e}")
    # Fallback to REST API
    try:
        resp = _get_with_retry(
            f"{TRONGRID_API}/v1/accounts/{address}",
            params={"only_confirmed": "true"},
        )
        if resp.status_code != 200:
            return 0.0
        data_list = resp.json().get("data", [])
        if not data_list:
            return 0.0
        data = data_list[0]
        for token in data.get("trc20", []):
            if USDT_CONTRACT in token:
                raw = int(token[USDT_CONTRACT])
                return raw / (10 ** USDT_DECIMALS)
        return 0.0
    except Exception as e:
        logger.error(f"Balance check error: {e}")
        return 0.0


def get_trx_balance(address: str) -> float:
    """Get TRX balance (for gas estimation)."""
    try:
        resp = _get_with_retry(f"{TRONGRID_API}/v1/accounts/{address}")
        if resp.status_code != 200:
            return 0.0
        data = resp.json().get("data", [{}])[0]
        return data.get("balance", 0) / 1_000_000  # sun -> TRX
    except Exception as e:
        logger.error(f"TRX balance error: {e}")
        return 0.0


def send_usdt(to_address: str, amount_usdt: float) -> dict:
    """
    Send USDT (TRC20) from hot wallet to address.
    Returns {"ok": True, "txid": ...} or {"ok": False, "error": ...}.
    """
    return send_usdt_from(_get_master_key(), to_address, amount_usdt)


def send_usdt_from(priv_key: PrivateKey, to_address: str, amount_usdt: float) -> dict:
    """
    Send USDT (TRC20) from a specific private key to address.
    Returns {"ok": True, "txid": ...} or {"ok": False, "error": ...}.
    """
    try:
        client = _get_client()
        contract = client.get_contract(USDT_CONTRACT)
        # USDT uses 6 decimals
        amount_units = int(round(amount_usdt * (10 ** USDT_DECIMALS)))
        txn = (
            contract.functions.transfer(to_address, amount_units)
            .with_owner(priv_key.public_key.to_base58check_address())
            .fee_limit(100_000_000)  # 100 TRX max fee
            .build()
            .sign(priv_key)
        )
        result = txn.broadcast()
        txid = result.get("txid", "")
        logger.info(f"USDT sent: {amount_usdt} -> {to_address}, txid={txid}")
        return {"ok": True, "txid": txid}
    except Exception as e:
        logger.error(f"USDT send failed: {e}")
        return {"ok": False, "error": str(e)}


def send_trx(to_address: str, amount_trx: float) -> dict:
    """Send TRX from hot wallet (for gas funding)."""
    try:
        client = _get_client()
        priv_key = _get_master_key()
        from_addr = priv_key.public_key.to_base58check_address()
        txn = (
            client.trx.transfer(from_addr, to_address, int(amount_trx * 1_000_000))
            .build()
            .sign(priv_key)
        )
        result = txn.broadcast()
        return {"ok": True, "txid": result.get("txid", "")}
    except Exception as e:
        logger.error(f"TRX send failed: {e}")
        return {"ok": False, "error": str(e)}


def ensure_gas(address: str, min_trx: float = 20.0) -> bool:
    """
    Ensure an address has enough TRX for gas.
    If not, send TRX from hot wallet automatically.
    If hot wallet is low, auto-swap USDT->TRX to refill.
    Returns True if gas is available (or was funded).
    """
    try:
        bal = get_trx_balance(address)
        if bal >= min_trx:
            return True
        # Check hot wallet, auto-refill if needed
        refill = auto_refill_gas(min_trx=30.0)
        if not refill.get("ok") and refill.get("action") not in ("sufficient", "swapped"):
            logger.warning(f"Auto-refill failed: {refill.get('error')}")
        logger.info(f"Funding gas: {address} has {bal} TRX, sending {min_trx}")
        result = send_trx(address, min_trx)
        if result.get("ok"):
            # Wait for confirmation
            time.sleep(10)
            return True
        return False
    except Exception as e:
        logger.error(f"ensure_gas failed: {e}")
        return False


def send_usdt_from_with_gas(priv_key: PrivateKey, to_address: str, amount_usdt: float) -> dict:
    """
    Send USDT from a derived address, auto-funding TRX for gas if needed.
    After payout, sweeps leftover TRX back to hot wallet (no waste!).
    This is the 'just works' version — user never manually sends TRX.
    """
    from_addr = priv_key.public_key.to_base58check_address()
    if not ensure_gas(from_addr):
        return {"ok": False, "error": "Could not fund gas (TRX). Hot wallet may be empty."}
    result = send_usdt_from(priv_key, to_address, amount_usdt)
    # Sweep leftover TRX back to hot wallet (don't waste it!)
    if result.get("ok"):
        try:
            time.sleep(5)  # let the USDT tx confirm
            leftover = get_trx_balance(from_addr)
            # Keep 1 TRX for account, sweep the rest
            if leftover > 1.5:
                sweep_amt = round(leftover - 1.0, 2)
                hot_addr = _get_master_key().public_key.to_base58check_address()
                client = _get_client()
                txn = (
                    client.trx.transfer(from_addr, hot_addr, int(sweep_amt * 1_000_000))
                    .build()
                    .sign(priv_key)
                )
                txn.broadcast()
                logger.info(f"Swept {sweep_amt} TRX back from {from_addr}")
        except Exception as e:
            logger.warning(f"TRX sweep-back failed (non-critical): {e}")
    return result


def swap_usdt_to_trx(amount_usdt: float, slippage_pct: float = 1.0) -> dict:
    """
    Swap USDT -> TRX via SunSwap V2 (for gas funding).
    Uses hot wallet. Returns {"ok": True, "trx_received": ..., "txid": ...}.
    """
    try:
        client = _get_client()
        priv_key = _get_master_key()
        from_addr = priv_key.public_key.to_base58check_address()

        # Get quote first (how much TRX we'll get)
        router = client.get_contract(SUNSWAP_V2_ROUTER)
        amount_in = int(amount_usdt * (10 ** USDT_DECIMALS))
        path = [USDT_CONTRACT, WTRX_CONTRACT]

        # Get amounts out
        amounts = router.functions.getAmountsOut(amount_in, path)
        expected_trx = int(amounts[-1]) / 1_000_000
        min_out = int(amounts[-1] * (1 - slippage_pct / 100))

        logger.info(f"Swapping {amount_usdt} USDT -> ~{expected_trx:.2f} TRX")

        # Step 1: Approve router to spend USDT
        usdt = client.get_contract(USDT_CONTRACT)
        approve_txn = (
            usdt.functions.approve(SUNSWAP_V2_ROUTER, amount_in)
            .with_owner(from_addr)
            .build()
            .sign(priv_key)
        )
        approve_txn.broadcast()
        time.sleep(5)  # wait for approval

        # Step 2: Swap
        deadline = int(time.time()) + 600  # 10 min
        swap_txn = (
            router.functions.swapExactTokensForTRX(
                amount_in, min_out, path, from_addr, deadline
            )
            .with_owner(from_addr)
            .build()
            .sign(priv_key)
        )
        result = swap_txn.broadcast()
        txid = result.get("txid", "")

        # Wait and check TRX received
        time.sleep(10)
        return {"ok": True, "trx_received": expected_trx, "txid": txid}
    except Exception as e:
        logger.error(f"Swap failed: {e}")
        return {"ok": False, "error": str(e)}


def auto_refill_gas(min_trx: float = 20.0, swap_usdt_amount: float = 10.0) -> dict:
    """
    If hot wallet TRX is below min_trx, auto-swap USDT->TRX to refill.
    Returns {"ok": True, "action": "swapped"/"sufficient"/"no_usdt"}.
    """
    hot_addr = _get_master_key().public_key.to_base58check_address()
    trx_bal = get_trx_balance(hot_addr)
    if trx_bal >= min_trx:
        return {"ok": True, "action": "sufficient", "trx": trx_bal}

    usdt_bal = get_usdt_balance(hot_addr)
    if usdt_bal < swap_usdt_amount:
        return {"ok": False, "action": "no_usdt",
                "error": f"Hot wallet low on TRX ({trx_bal:.1f}) and USDT ({usdt_bal:.1f})"}

    logger.info(f"Auto-refill: swapping {swap_usdt_amount} USDT -> TRX (hot wallet low: {trx_bal:.1f} TRX)")
    result = swap_usdt_to_trx(swap_usdt_amount)
    if result.get("ok"):
        return {"ok": True, "action": "swapped", "trx_received": result.get("trx_received")}
    return {"ok": False, "action": "swap_failed", "error": result.get("error")}


def wait_for_deposit(address: str, expected_usdt: float, timeout_sec: int = 7200,
                     poll_interval: int = 10, tolerance: float = 0.01) -> dict:
    """
    Poll TronGrid for an incoming USDT deposit >= expected (minus tolerance).
    Returns {"ok": True, "amount": ..., "txid": ...} on success,
    or {"ok": False, "error": "timeout"} on timeout.
    NOTE: v1 tracks by amount; ensure expected amounts are unique per deal
    or use short timeouts per deal.
    """
    start = time.time()
    seen_txids: set = set()
    # Baseline: current TRC20 transfers to avoid counting old ones
    try:
        resp = _get_with_retry(
            f"{TRONGRID_API}/v1/accounts/{address}/transactions/trc20",
            params={"limit": 20, "only_confirmed": "true",
                    "contract_address": USDT_CONTRACT},
        )
        if resp.status_code == 200:
            for t in resp.json().get("data", []):
                seen_txids.add(t.get("transaction_id", ""))
    except Exception as e:
        logger.warning(f"Baseline fetch failed: {e}")

    while time.time() - start < timeout_sec:
        try:
            resp = _get_with_retry(
                f"{TRONGRID_API}/v1/accounts/{address}/transactions/trc20",
                params={"limit": 20, "only_confirmed": "true",
                        "contract_address": USDT_CONTRACT},
            )
            if resp.status_code == 200:
                for t in resp.json().get("data", []):
                    txid = t.get("transaction_id", "")
                    if txid in seen_txids:
                        continue
                    # Only incoming transfers (to == address)
                    if t.get("to", "") != address:
                        continue
                    raw_val = t.get("value", "0")
                    amount = int(raw_val) / (10 ** USDT_DECIMALS)
                    if amount >= expected_usdt - tolerance:
                        logger.info(f"Deposit detected: {amount} USDT, txid={txid}")
                        return {"ok": True, "amount": amount, "txid": txid}
                    seen_txids.add(txid)
        except Exception as e:
            logger.warning(f"Deposit poll error: {e}")
        time.sleep(poll_interval)
    return {"ok": False, "error": "timeout"}


def check_txid_confirmed(txid: str) -> bool:
    """Check if a transaction is confirmed on-chain."""
    try:
        resp = _get_with_retry(f"{TRONGRID_API}/v1/transactions/{txid}")
        if resp.status_code != 200:
            return False
        data = resp.json().get("data", [{}])[0]
        return data.get("blockNumber") is not None
    except Exception:
        return False
