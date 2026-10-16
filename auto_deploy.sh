#!/bin/bash
# Auto-deploy watcher — har 5 minute me naye code ke liye check karta hai
# Setup: is file ko /root/auto_deploy.sh me rakho aur cron me lagao:
#   */5 * * * * /root/auto_deploy.sh >> /root/auto_deploy.log 2>&1

REPO_URL="https://github.com/wherearemyfunds-hue/Baloch-bots.git"
REPO_DIR="/root/baloch-bots"
VERSION_FILE="/root/.bot_version"

# Kill a bot reliably: SIGTERM, wait up to 10s for it to die, then SIGKILL.
# A half-dead telethon process keeps the .session sqlite file locked, which
# makes the fresh instance crash with "database is locked" - leaving NO bot
# running at all.
kill_bot() {
  local pattern="$1"
  pkill -f "$pattern" 2>/dev/null
  for i in $(seq 1 10); do
    pgrep -f "$pattern" >/dev/null 2>&1 || return 0
    sleep 1
  done
  pkill -9 -f "$pattern" 2>/dev/null
  sleep 1
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "WARNING: $pattern still alive after SIGKILL!"
    return 1
  fi
  return 0
}

# Pehli baar: repo clone karo
if [ ! -d "$REPO_DIR" ]; then
  echo "[$(date)] Cloning repo..."
  git clone "$REPO_URL" "$REPO_DIR" || { echo "Clone failed!"; exit 1; }
fi

cd "$REPO_DIR" || exit 1

# Naya code check karo (reset --hard taake local changes kabhi block na karein)
git fetch origin main 2>/dev/null
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
  echo "[$(date)] New code found! Deploying..."
  git reset --hard origin/main || { echo "Reset failed!"; exit 1; }

  # Self-update: taake is script ke future fixes bhi agle run se lag jayen
  # NOTE: cp ki jagah atomic mv - chalti hui script ko cp se overwrite
  # karne par bash purani line offsets par NAYI content parhta hai aur
  # "command not found" / syntax errors se crash hota hai. mv naya inode
  # banata hai, chalti script purani file par sahi rehti hai.
  cp "$REPO_DIR/auto_deploy.sh" /tmp/auto_deploy.sh.new
  mv /tmp/auto_deploy.sh.new /root/auto_deploy.sh
  chmod +x /root/auto_deploy.sh

  # Env files safe tareeqe se load karo (seed phrase me spaces hote hain,
  # is liye kabhi `export $(cat | xargs)` use na karo).
  # NOTE: function ki jagah inline likha hai taake purana script bhi
  # ye update lagate waqt break na ho.

  # Ensure tronpy and bip-utils are installed (for Direct TRON USDT)
  pip3 show tronpy >/dev/null 2>&1 || pip3 install --break-system-packages tronpy 2>&1 | tail -1
  pip3 show bip-utils >/dev/null 2>&1 || pip3 install --break-system-packages bip-utils 2>&1 | tail -1
  # web3 for BNB Smart Chain USDT (BEP20) in the wallet bot
  # NOTE: pin web3<8 — v8 pulls eth-abi 6.x which breaks tronpy (needs eth-abi<6).
  # web3 v7 works with eth-abi 5.x alongside tronpy.
  pip3 show web3 >/dev/null 2>&1 || pip3 install --break-system-packages "web3<8" 2>&1 | tail -1
  # Guard: web3's dependency install can downgrade pydantic-core and break
  # aiogram at startup (SystemError: pydantic-core incompatible). Restore it.
  python3 -c "import pydantic" 2>/dev/null || \
    pip3 install --break-system-packages --force-reinstall --no-deps "pydantic-core==2.50.0" 2>&1 | tail -1

  # Shared chain helper modules FIRST — both bots import these, so they must
  # be fresh on disk before any bot restarts (stale modules caused a
  # production "No module named 'tx_log'" crash before).
  [ -f tron_pay.py ] && cp tron_pay.py /root/tron_pay.py
  [ -f bsc_pay.py ] && cp bsc_pay.py /root/bsc_pay.py
  [ -f tx_log.py ] && cp tx_log.py /root/tx_log.py
  python3 -m py_compile /root/tron_pay.py || { echo "TRON module syntax failed!"; exit 1; }
  python3 -m py_compile /root/bsc_pay.py || { echo "BSC module syntax failed!"; exit 1; }

  # Deal Manager Bot
  cp dealmanagerbot.py /root/dealmanagerbot.py
  python3 -m py_compile /root/dealmanagerbot.py || { echo "Syntax failed!"; exit 1; }
  kill_bot dealmanagerbot.py
  set -a; source /root/.secrets/deal_manager.env; set +a
  nohup python3 /root/dealmanagerbot.py > /root/dealbot.log 2>&1 &

  # Escrow Bot
  cp dealcreaterbot.py /root/dealcreaterbot.py
  python3 -m py_compile /root/dealcreaterbot.py || { echo "Syntax failed!"; exit 1; }
  kill_bot dealcreaterbot.py
  set -a; source /root/.secrets/escrow_bot.env; set +a
  nohup python3 /root/dealcreaterbot.py > /root/escrowbot.log 2>&1 &

  # Wallet Bot (Direct TRON USDT + BNB Smart Chain USDT)
  if [ -f walletbot.py ]; then
    cp walletbot.py /root/walletbot.py
    python3 -m py_compile /root/walletbot.py || { echo "Wallet syntax failed!"; exit 1; }
    kill_bot walletbot.py
    if [ -f /root/.secrets/wallet_bot.env ]; then
      set -a; source /root/.secrets/wallet_bot.env; set +a
      nohup python3 /root/walletbot.py > /root/walletbot.log 2>&1 &
      echo "[$(date)] Wallet bot restarted."
    else
      echo "[$(date)] Wallet bot skipped: /root/.secrets/wallet_bot.env missing."
    fi
  fi

  echo "$REMOTE" > "$VERSION_FILE"
  echo "[$(date)] Deploy complete!"
else
  echo "[$(date)] No updates."
fi
# Auto-deploy trigger
