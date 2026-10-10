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

# Naya code check karo
git fetch origin main 2>/dev/null
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
  echo "[$(date)] New code found! Deploying..."
  git pull origin main || { echo "Pull failed!"; exit 1; }

  # Deal Manager Bot
  cp dealmanagerbot.py /root/dealmanagerbot.py
  python3 -m py_compile /root/dealmanagerbot.py || { echo "Syntax failed!"; exit 1; }
  pkill -f dealmanagerbot.py
  sleep 2
  export $(cat /root/.secrets/deal_manager.env | xargs)
  nohup python3 /root/dealmanagerbot.py > /root/dealbot.log 2>&1 &

  # Escrow Bot
  cp dealcreaterbot.py /root/dealcreaterbot.py
  python3 -m py_compile /root/dealcreaterbot.py || { echo "Syntax failed!"; exit 1; }
  pkill -f dealcreaterbot.py
  sleep 2
  export $(cat /root/.secrets/escrow_bot.env | xargs)
  nohup python3 /root/dealcreaterbot.py > /root/escrowbot.log 2>&1 &

  echo "$REMOTE" > "$VERSION_FILE"
  echo "[$(date)] Deploy complete!"
else
  echo "[$(date)] No updates."
fi
