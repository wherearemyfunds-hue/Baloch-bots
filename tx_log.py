"""Transaction history log for the wallet/deal bots.

Every outgoing (and detected incoming) transaction is appended here so the
admin can review history from the wallet bot's 📜 History button.
File: tx_history.json next to this module. Logging never raises — a logging
failure must never break a money movement.
"""
import json
import os
import threading
import time

_TX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tx_history.json")
_lock = threading.Lock()
_KEEP = 500  # keep last N entries


def _load():
    try:
        with open(_TX_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(txs):
    tmp = _TX_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(txs[-_KEEP:], f)
    os.replace(tmp, _TX_FILE)


def log_tx(chain, tx_type, amount, asset, from_addr, to_addr, txid, note=""):
    """Append one transaction. Never raises.

    chain: "tron" | "bsc"
    tx_type: "deposit" | "withdraw" | "payout" | "swap" | "gas"
    asset: "USDT" | "BNB" | "TRX"
    """
    try:
        with _lock:
            txs = _load()
            txs.append({
                "ts": time.time(),
                "chain": chain,
                "type": tx_type,
                "amount": round(float(amount), 6),
                "asset": asset,
                "from": from_addr or "",
                "to": to_addr or "",
                "txid": txid or "",
                "note": note or "",
            })
            _save(txs)
    except Exception:
        pass


def get_history(limit=15):
    """Recent transactions, newest first."""
    try:
        return list(reversed(_load()[-limit:]))
    except Exception:
        return []
