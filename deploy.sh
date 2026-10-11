#!/bin/bash
# Auto-deploy both bots — Shawn ke liye one-command deploy
cd /root

echo "=== Secrets load ==="
# Dono bots env variables mangte hain (khud .env load nahi karte).
# /root/.secrets/*.env se load karo taake har deploy ke baad bots crash na hon.
set -a
for f in /root/.secrets/*.env; do
  [ -f "$f" ] && . "$f" && echo "loaded: $f"
done
set +a

echo "=== Telethon check ==="
# Aaj telethon wipe ho gaya tha — deploy se pehle ensure karo
python3 -c "import telethon" 2>/dev/null || pip3 install -q --break-system-packages telethon
python3 -c "import telethon; print('telethon OK', telethon.__version__)"

echo "=== Deal Manager Bot ==="
wget -q -O dealmanagerbot.py "https://muse.ai/files/1424736604046464/3396694040512694/8j8c7orp7vuir8knacfu71hy/dealmanagerbot.py"
python3 -m py_compile dealmanagerbot.py && echo "SYNTAX OK" || { echo "SYNTAX FAILED"; exit 1; }
pkill -f dealmanagerbot.py
sleep 2
nohup python3 dealmanagerbot.py > /root/dealbot.log 2>&1 &
sleep 3
echo "=== Escrow Bot (no limit) ==="
wget -q -O dealcreaterbot.py "https://muse.ai/files/1424736604046464/1408554064718294/zpccc4qwe7h7ajgjc5ytcohg/dealcreaterbot.py"
python3 -m py_compile dealcreaterbot.py && echo "SYNTAX OK" || { echo "SYNTAX FAILED"; exit 1; }
pkill -f dealcreaterbot.py
sleep 2
nohup python3 dealcreaterbot.py > /root/escrowbot.log 2>&1 &
sleep 3
echo "=== Status ==="
ps aux | grep -E "dealmanagerbot|dealcreaterbot" | grep -v grep
echo "Done!"
