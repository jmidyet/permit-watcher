# CLAUDE.md — agent guide for permit-watcher

Operational notes for an AI agent working on this repo. Read this before making
changes or deploying.

## What this is
A recreation.gov permit-cancellation watcher. It polls availability APIs and
sends a Telegram message when an opening appears. Runs as a `--once` script
fired by cron on a DigitalOcean droplet.

## Layout
- `src/main.py` — entry point. `--once` = one check then exit (cron uses this);
  no flag = long-running interval scheduler.
- `src/permit_checker.py` — fetches/parses availability, decides what to alert.
- `src/notifier.py` — Telegram (+ optional email/SMS) sending.
- `src/config_loader.py` — loads YAML + `.env`.
- `src/scheduler.py` — APScheduler interval/time scheduler (only used without `--once`).
- `src/telegram_listener.py` — `--telegram-poll` handler: reads a command from the
  bot and replies with the current openings (manual run; ignores dedup state).
- `config/permits.yaml` — which permits/segments/windows to watch.
- `config/config.yaml` — request rate limits, notifications, scheduler, `state_file`.
- `deploy/setup.sh` — one-time droplet setup; prints the cron lines.
- `utils/get_telegram_chat_id.py` — helper to find your chat id.

## Two recreation.gov APIs — IMPORTANT semantics
Each permit sets `api:` in `permits.yaml`:
- `month` (rivers/standard): `GET /api/permits/{id}/availability/month?start_date=YYYY-MM-01T00:00:00.000Z&commercial_acct=false`.
  `remaining` = number of **launches/permits** (one covers the whole group), so
  `remaining >= 1` is enough.
- `inyo` (Inyo NF + SEKI wilderness): `GET /api/permitinyo/{id}/availabilityv2?start_date=YYYY-MM-DD&commercial_acct=false`.
  `remaining` (under `quota_usage_by_member_daily`) = **per-person quota**, so a
  party needs `remaining >= people`.

`_check_permit` encodes this: default `min_remaining` is `1` for `month` and
`people` for `inyo`; override per-permit with `min_remaining:`.

Send a browser `User-Agent` on any manual curl or you may get blocked.
Division ids (the `divisions:` filter) are the keys under the availability
payload; human names come from `GET /api/permitcontent/{id}`.

## Config model (permits.yaml per permit)
`name`, `id`, `api` (`month`|`inyo`), `dates.{start,end}`, `people`,
`divisions` (list of numeric ids to watch), `division_names` (id->label shown in
alerts), optional `min_remaining`. Global `search_options.min_lead_days` skips
openings sooner than N days out (default 2 ≈ 48h). Past dates are always skipped.

## Notifications & state
- Telegram needs `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` (gitignored).
- `state.json` (gitignored) maps `permitid_divisionid -> {dates, notified_at}`.
  One message per segment listing all open dates; only **new** dates trigger a
  re-alert, so frequent cron runs don't spam. Empty segments are dropped so a
  reopening re-alerts. Deleting `state.json` forces a fresh full list.

## Making a change (dev loop)
1. Edit locally. Test against the live API WITHOUT sending Telegram: use a stub
   notifier + a temp `state_path` and zeroed delays. Example:
   ```python
   import logging; logging.basicConfig(level=logging.ERROR)
   from src.config_loader import ConfigLoader
   from src.permit_checker import PermitChecker
   cfg=ConfigLoader("config"); app=cfg.load_app_config(); permits=cfg.load_permits_config()
   app['request']['delay']={'min':0,'max':0}; app['request']['jitter']=0
   class Stub:
       def send_notification(self, subject, message): print(message,"\n")
   PermitChecker(app, permits, Stub(), state_path="/tmp/pw_test.json").check_all_permits()
   ```
   Do NOT run `python src/main.py --once` during dev — it sends real Telegrams.
2. Commit + push to `origin/main` (repo: github.com/jmidyet/permit-watcher, public).

## Deploying
Deploy = push, then pull on the droplet. No service/daemon to restart — cron
runs the latest code on its next tick.
```bash
ssh root@<DROPLET_IP> 'cd /root/permit-watcher && git pull -q && echo at $(git rev-parse --short HEAD)'
```
- Droplet: Ubuntu 24.04, Python 3.12, app at `/root/permit-watcher`, venv at
  `./venv`. The actual `<DROPLET_IP>` is intentionally kept out of this public
  repo (it's in agent memory / ask the user).
- If `requirements.txt` changed: `ssh root@<DROPLET_IP> '/root/permit-watcher/venv/bin/pip install -q -r /root/permit-watcher/requirements.txt'`.
- To update secrets: `scp .env root@<DROPLET_IP>:/root/permit-watcher/.env`.

### Python 3.12 dependency gotchas (already pinned — keep them)
- `pyyaml` must be `>=6.0.2` (6.0 fails to build on 3.12).
- `setuptools<81` (apscheduler 3.10 imports `pkg_resources`, removed in
  setuptools 81; 3.12 venvs omit it by default).

## Cron on the droplet
System timezone is `America/Los_Angeles` (so cron times are Pacific, DST-aware).
```cron
*/10 * * * * /usr/bin/flock -n /tmp/permit-watcher.lock /root/permit-watcher/venv/bin/python /root/permit-watcher/src/main.py --once >> /root/permit-watcher/cron.log 2>&1
0 18 * * 3 rm -f /root/permit-watcher/state.json && /usr/bin/flock -n /tmp/permit-watcher.lock /root/permit-watcher/venv/bin/python /root/permit-watcher/src/main.py --once >> /root/permit-watcher/cron.log 2>&1
* * * * * /usr/bin/flock -n /tmp/permit-watcher-tg.lock /root/permit-watcher/venv/bin/python /root/permit-watcher/src/main.py --telegram-poll >> /root/permit-watcher/cron.log 2>&1
```
- `flock` prevents overlapping runs (a full pass takes ~1-2 min). The Telegram
  poll uses a separate lock so it doesn't serialize with the watcher.
- Weekly line wipes state Wed 6pm Pacific, then re-checks → fresh full list.
- Manual run: user messages the bot `/run` (or `/check`/`/status`); the
  every-minute `--telegram-poll` replies with all current openings (ignores
  state). Offset tracked in `telegram_offset.json` (gitignored).
- Logs: `tail -f /root/permit-watcher/cron.log`. Inspect dedup: `cat state.json`.

## Quick ops
- Force a fresh list now: `ssh root@<DROPLET_IP> 'rm -f /root/permit-watcher/state.json'` (next tick re-sends everything current).
- Change check frequency: `crontab -e`, edit `*/10`.
- Verify a permit/division live (with a browser UA):
  `curl -s -H 'User-Agent: Mozilla/5.0' 'https://www.recreation.gov/api/permits/233393/availability/month?start_date=2026-07-01T00:00:00.000Z&commercial_acct=false'`
