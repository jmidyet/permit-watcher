#!/usr/bin/env bash
#
# One-time setup for running permit-watcher on a fresh Ubuntu droplet via cron.
# Run this from inside the cloned repo directory:
#
#     cd permit-watcher && bash deploy/setup.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Installing system packages (python venv + git)…"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y python3-venv python3-pip git
fi

echo "==> Creating virtualenv and installing dependencies…"
python3 -m venv venv
./venv/bin/pip install --upgrade pip >/dev/null
./venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from template — EDIT IT NOW with your Telegram token + chat id:"
  echo "      nano $REPO_DIR/.env"
fi

PY="$REPO_DIR/venv/bin/python"
LOCK="/usr/bin/flock -n /tmp/permit-watcher.lock"
# flock ensures a slow run never overlaps the next scheduled one.
CRON_LINE="*/10 * * * * $LOCK $PY $REPO_DIR/src/main.py --once >> $REPO_DIR/cron.log 2>&1"
# Weekly: wipe state so the full current list is re-sent (Wed 6pm box time).
WIPE_LINE="0 18 * * 3 rm -f $REPO_DIR/state.json && $LOCK $PY $REPO_DIR/src/main.py --once >> $REPO_DIR/cron.log 2>&1"

cat <<EOF

==> Setup complete.

Next steps:
  1. Edit your secrets:        nano $REPO_DIR/.env
  2. Find your chat id:        $PY utils/get_telegram_chat_id.py
  3. Test one run:             $PY $REPO_DIR/src/main.py --once
  4. (optional) make cron times local + DST-correct:
       sudo timedatectl set-timezone America/Los_Angeles && sudo systemctl restart cron
  5. Add cron jobs:            crontab -e   (paste both lines below)

     # check every 10 minutes (change */10 to */5, */2, etc.)
     $CRON_LINE
     # weekly fresh list: wipe state Wed 6pm (box timezone) and re-check
     $WIPE_LINE

To watch it work:  tail -f $REPO_DIR/cron.log
EOF
