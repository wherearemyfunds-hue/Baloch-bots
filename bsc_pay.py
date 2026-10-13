#!/usr/bin/env python3
"""
BNB Smart Chain USDT (BEP20) payments — mirrors tron_pay.py API.
- USDT BEP20 contract (BSC mainnet): 0x55d398326f99059fF775485246999027B3197955
- Same WALLET_SEED_PHRASE derives BSC deposit addresses: m/44'/60'/0'/0/{index}
  (BSC is EVM-compatible, so the standard Ethereum BIP44 path is used)
- Hot wallet = the same TRON_PRIVATE_KEY bytes used as a secp256k1 key on BSC
  (TRON keys are secp256k1, so the raw 32 bytes are a valid BSC private key).
  => No new secret needed. Fund this BSC address with a little BNB for gas.
- Gas (BNB) auto-funded from hot wallet; leftover BNB swept back after sends.
Requires: pip install web3
"""
import logging
import os
import time

logger = logging.getLogger("bsc_pay")

# USDT BEP20 on BSC mainnet
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
# BEP20 USDT uses 18 decimals (unlike TRC20 USDT's 6). Was wrongly 6, which
# made balances show 1e12x too large and sends transfer dust.
USDT_DECIMALS = 18
# Chain ID for BSC mainnet
BSC_CHAIN_ID = 56
# Transfer(address,address,uint256) event topic (well-known constant)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# Minimal ERC20 ABI (balanceOf / transfer / decimals)
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
     "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"},
                                   {"name": "_value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}],
     "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]

_w3 = None
_hot_acct = None
_hd_ctx = None


def _rpc_url() -> str:
    return os.environ.get("BSC_RPC_URL", "https://bsc-dataseed.binance.org/").strip()


def _get_w3():
    """Lazy Web3 client for BSC."""
    global _w3
    if _w3 is None:
        from web3 import Web3
        _w3 = Web3(Web3.HTTPProvider(_rpc_url(), request_kwargs={"timeout": 20}))
    return _w3


def _get_hot_acct():
    """Hot wallet account: same raw key bytes as TRON_PRIVATE_KEY."""
    global _hot_acct
    if _hot_acct is None:
        from web3 import Web3
        hex_key = os.environ.get("TRON_PRIVATE_KEY", "").strip()
        if not hex_key:
            raise RuntimeError("TRON_PRIVATE_KEY not set in environment")
        if not hex_key.startswith("0x"):
            hex_key = "0x" + hex_key
        _hot_acct = Web3().eth.account.from_key(hex_key)
    return _hot_acct


def get_hot_wallet_address() -> str:
    """Return the hot wallet BSC (0x...) address."""
    return _get_hot_acct().address


def _get_hd_ctx():
    """BIP44 HD context from seed phrase (Ethereum path — same for BSC)."""
    global _hd_ctx
    if _hd_ctx is None:
        from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
        seed_phrase = os.environ.get("WALLET_SEED_PHRASE", "").strip()
        if not seed_phrase:
            raise RuntimeError("WALLET_SEED_PHRASE not set — needed for per-user addresses")
        seed = Bip39SeedGenerator(seed_phrase).Generate()
        _hd_ctx = Bip44.FromSeed(seed, Bip44Coins.ETHEREUM).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT)
    return _hd_ctx


def get_user_deposit_address(user_index: int) -> str:
    """
    Derive a unique BSC deposit address for a user.
    Path: m/44'/60'/0'/0/{user_index}  (same index as the user's TRON address)
    """
    ctx = _get_hd_ctx()
    return ctx.AddressIndex(user_index).PublicKey().ToAddress()


def get_user_private_key_hex(user_index: int) -> str:
    """Raw private key hex (0x...) for a user's derived BSC address."""
    ctx = _get_hd_ctx()
    raw_hex = ctx.AddressIndex(user_index).PrivateKey().Raw().ToHex()
    return "0x" + raw_hex


def _usdt_contract():
    from web3 import Web3
    w3 = _get_w3()
    return w3.eth.contract(
        address=Web3.to_checksum_address(USDT_CONTRACT), abi=ERC20_ABI)


def get_usdt_balance(address: str) -> float:
    """USDT (BEP20) balance."""
    try:
        from web3 import Web3
        c = _usdt_contract()
        raw = c.functions.balanceOf(Web3.to_checksum_address(address)).call()
        return int(raw) / (10 ** USDT_DECIMALS)
    except Exception as e:
        logger.error(f"BSC USDT balance error: {e}")
        return 0.0


def get_bnb_balance(address: str) -> float:
    """BNB balance (for gas)."""
    try:
        from web3 import Web3
        w3 = _get_w3()
        wei = w3.eth.get_balance(Web3.to_checksum_address(address))
        return wei / 1e18
    except Exception as e:
        logger.error(f"BNB balance error: {e}")
        return 0.0


def send_bnb(to_address: str, amount_bnb: float, retries: int = 3) -> dict:
    """Send BNB from hot wallet (for gas funding). Retries on RPC hiccups
    (the public BSC endpoint throttles aggressively)."""
    from web3 import Web3
    last_err = ""
    for attempt in range(retries):
        try:
            w3 = _get_w3()
            acct = _get_hot_acct()
            tx = {
                "from": acct.address,
                "to": Web3.to_checksum_address(to_address),
                "value": int(amount_bnb * 1e18),
                "gas": 21000,
                "gasPrice": w3.eth.gas_price,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "chainId": BSC_CHAIN_ID,
            }
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            return {"ok": True, "txid": w3.to_hex(tx_hash)}
        except Exception as e:
            last_err = str(e)
            logger.warning(f"BNB send attempt {attempt + 1}/{retries} failed: {e}")
            time.sleep(2 * (attempt + 1))
    logger.error(f"BNB send failed after {retries} tries: {last_err}")
    return {"ok": False, "error": last_err}


def ensure_gas(address: str, min_bnb: float = 0.004) -> bool:
    """
    Ensure an address has enough BNB for gas.
    If not, fund from hot wallet automatically.
    A BEP20 USDT transfer costs ~$0.10-0.30, so 0.004 BNB is plenty.
    """
    try:
        bal = get_bnb_balance(address)
        if bal >= min_bnb:
            return True
        # Sanity check: is the hot wallet actually funded?
        hot_bal = get_bnb_balance(_get_hot_acct().address)
        if hot_bal < min_bnb:
            logger.error(f"BSC ensure_gas: hot wallet has {hot_bal} BNB, need {min_bnb} — truly empty")
            return False
        logger.info(f"Funding BSC gas: {address} has {bal} BNB, sending {min_bnb} (hot wallet: {hot_bal})")
        result = send_bnb(address, min_bnb)
        if result.get("ok"):
            time.sleep(8)  # let it confirm (~3s blocks)
            return True
        return False
    except Exception as e:
        logger.error(f"BSC ensure_gas failed: {e}")
        return False


def send_usdt_from(priv_key_hex: str, to_address: str, amount_usdt: float) -> dict:
    """Send USDT (BEP20) from a specific private key."""
    try:
        from web3 import Web3
        w3 = _get_w3()
        acct = w3.eth.account.from_key(priv_key_hex)
        c = _usdt_contract()
        amount_units = int(round(amount_usdt * (10 ** USDT_DECIMALS)))
        tx = c.functions.transfer(
            Web3.to_checksum_address(to_address), amount_units
        ).build_transaction({
            "from": acct.address,
            "gas": 120000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": BSC_CHAIN_ID,
        })
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        txid = w3.to_hex(tx_hash)
        logger.info(f"BSC USDT sent: {amount_usdt} -> {to_address}, txid={txid}")
        return {"ok": True, "txid": txid}
    except Exception as e:
        logger.error(f"BSC USDT send failed: {e}")
        return {"ok": False, "error": str(e)}


def send_usdt(to_address: str, amount_usdt: float) -> dict:
    """Send USDT (BEP20) from hot wallet."""
    return send_usdt_from(_get_hot_acct().key.hex(), to_address, amount_usdt)


def send_usdt_from_with_gas(priv_key_hex: str, from_address: str,
                            to_address: str, amount_usdt: float) -> dict:
    """
    Send USDT from a derived address, auto-funding BNB for gas if needed.
    After payout, sweeps leftover BNB back to hot wallet (no waste!).
    """
    if not ensure_gas(from_address):
        return {"ok": False, "error": "Could not fund gas (BNB). Hot wallet may be empty."}
    result = send_usdt_from(priv_key_hex, to_address, amount_usdt)
    # Sweep leftover BNB back to hot wallet
    if result.get("ok"):
        try:
            from web3 import Web3
            w3 = _get_w3()
            time.sleep(6)
            leftover = get_bnb_balance(from_address)
            # Keep 0.001 BNB reserve, sweep the rest
            if leftover > 0.002:
                sweep_amt = leftover - 0.001
                acct = w3.eth.account.from_key(priv_key_hex)
                hot = get_hot_wallet_address()
                tx = {
                    "from": acct.address,
                    "to": Web3.to_checksum_address(hot),
                    "value": int(sweep_amt * 1e18),
                    "gas": 21000,
                    "gasPrice": w3.eth.gas_price,
                    "nonce": w3.eth.get_transaction_count(acct.address),
                    "chainId": BSC_CHAIN_ID,
                }
                signed = acct.sign_transaction(tx)
                w3.eth.send_raw_transaction(signed.rawTransaction)
                logger.info(f"Swept {sweep_amt:.5f} BNB back from {from_address}")
        except Exception as e:
            logger.warning(f"BNB sweep-back failed (non-critical): {e}")
    return result


def get_latest_block() -> int:
    """Current BSC block number (0 on error)."""
    try:
        return int(_get_w3().eth.block_number)
    except Exception as e:
        logger.warning(f"BSC get_latest_block failed: {e}")
        return 0


def get_new_deposits(address: str, from_block: int) -> tuple:
    """
    Fetch incoming USDT Transfer events to address since from_block.
    Returns (deposits, latest_block) where deposits = [(amount, txid), ...].
    Uses raw eth_getLogs so it works across web3.py versions.
    """
    try:
        from web3 import Web3
        w3 = _get_w3()
        latest = w3.eth.block_number
        if from_block >= latest:
            return [], latest
        # Cap the scan window to avoid huge queries
        if latest - from_block > 5000:
            from_block = latest - 5000
        addr_topic = "0x000000000000000000000000" + address[2:].lower()
        logs = w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": "latest",
            "address": Web3.to_checksum_address(USDT_CONTRACT),
            "topics": [TRANSFER_TOPIC, None, addr_topic],
        })
        deposits = []
        for lg in logs:
            try:
                amount = int(lg["data"].hex(), 16) / (10 ** USDT_DECIMALS)
                txid = w3.to_hex(lg["transactionHash"])
                deposits.append((amount, txid))
            except Exception:
                continue
        return deposits, latest
    except Exception as e:
        logger.warning(f"BSC deposit scan error: {e}")
        return [], from_block
