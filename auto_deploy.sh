#!/bin/bash
# Auto-deploy watcher — har 5 minute me naye code ke liye check karta hai
# Setup: is file ko /root/auto_deploy.sh me rakho aur cron me lagao:
#   */5 * * * * /root/auto_deploy.sh >> /root/auto_deploy.log 2>&1

REPO_URL="https://github.com/wherearemyfunds-hue/Baloch-bots.git"
REPO_DIR="/root/baloch-bots"
VERSION_FILE="/root/.bot_version"

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
  cp "$REPO_DIR/auto_deploy.sh" /root/auto_deploy.sh
  chmod +x /root/auto_deploy.sh

  # Env files safe tareeqe se load karo (seed phrase me spaces hote hain,
  # is liye kabhi `export $(cat | xargs)` use na karo)
  load_env() {
    set -a
    # shellcheck disable=SC1090
    source "$1"
    set +a
  }

  # Ensure tronpy and bip-utils are installed (for Direct TRON USDT)
  pip3 show tronpy >/dev/null 2>&1 || pip3 install --break-system-packages tronpy 2>&1 | tail -1
  pip3 show bip-utils >/dev/null 2>&1 || pip3 install --break-system-packages bip-utils 2>&1 | tail -1

  # Deal Manager Bot
  cp dealmanagerbot.py /root/dealmanagerbot.py
  python3 -m py_compile /root/dealmanagerbot.py || { echo "Syntax failed!"; exit 1; }
  pkill -f dealmanagerbot.py
  sleep 2
  load_env /root/.secrets/deal_manager.env
  nohup python3 /root/dealmanagerbot.py > /root/dealbot.log 2>&1 &

  # Escrow Bot
  cp dealcreaterbot.py /root/dealcreaterbot.py
  python3 -m py_compile /root/dealcreaterbot.py || { echo "Syntax failed!"; exit 1; }
  pkill -f dealcreaterbot.py
  sleep 2
  load_env /root/.secrets/escrow_bot.env
  nohup python3 /root/dealcreaterbot.py > /root/escrowbot.log 2>&1 &

  # Wallet Bot (BTCPay)
  if [ -f walletbot.py ]; then
    cp walletbot.py /root/walletbot.py
    python3 -m py_compile /root/walletbot.py || { echo "Wallet syntax failed!"; exit 1; }
    pkill -f walletbot.py
    sleep 2
    if [ -f /root/.secrets/wallet_bot.env ]; then
      load_env /root/.secrets/wallet_bot.env
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
