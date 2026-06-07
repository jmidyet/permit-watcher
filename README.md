# Permit Watcher

Monitors [recreation.gov](https://www.recreation.gov) for permit openings (mostly
cancellations) and sends a **Telegram** message the moment a spot appears.

It polls the same availability APIs the recreation.gov website uses, with polite
rate limiting and randomized user agents. A single open entry/launch date is all
that's needed — one permit covers a whole trip — so it alerts on any open day in
the windows you configure.

## What it watches (out of the box)

Configured in `config/permits.yaml`:

| Permit | API | id | division(s) |
|---|---|---|---|
| Desolation / Gray Canyon — Green River | month | 233393 | 282 |
| Dinosaur — Gates of Lodore (Green River) | month | 250014 | 380 |
| Dinosaur — Yampa River | month | 250014 | 371 |
| Big Pine Creek — North Fork (overnight) | inyo | 233262 | 495 |
| Rae Lakes Loop — Woods Creek / Bubbs Creek | inyo | 445857 | 44585703, 44585704 |

recreation.gov exposes two different availability APIs; each permit sets `api:` to
`month` (rivers / standard permits) or `inyo` (Inyo & SEKI wilderness permits).

## How it runs

There are two modes:

- **`python src/main.py`** — long-running; uses the in-app interval scheduler
  (`config/config.yaml` → `scheduler.interval`, in minutes).
- **`python src/main.py --once`** — one check, then exit. This is what cron calls.

The recommended deployment is a small Linux box + cron calling `--once`, which lets
you check at **any** frequency (cron goes down to every minute). Already-alerted
openings are remembered in `state.json` so you don't get re-notified every run.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env with your Telegram token + chat id
python utils/get_telegram_chat_id.py   # helper to find your chat id

python src/main.py --once   # one test run
```

### Telegram setup
1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.
2. Send your new bot any message so it can see your chat.
3. Run `python utils/get_telegram_chat_id.py` to get your chat id.
4. Put both into `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Deploy: DigitalOcean droplet + cron (~$4–6/mo)

1. Create the smallest Ubuntu droplet on DigitalOcean.
2. SSH in and grab the code:
   ```bash
   git clone <your-repo-url> permit-watcher
   cd permit-watcher
   bash deploy/setup.sh
   ```
   `setup.sh` installs deps into a venv, creates `.env`, and prints a ready-to-use
   cron line.
3. Edit `.env` with your Telegram credentials (`nano .env`).
4. Test once: `./venv/bin/python src/main.py --once`
5. Add the cron job with `crontab -e`, e.g. every 10 minutes:
   ```cron
   */10 * * * * cd /root/permit-watcher && /root/permit-watcher/venv/bin/python src/main.py --once >> /root/permit-watcher/cron.log 2>&1
   ```
   Change `*/10` to `*/5`, `*/2`, etc. to check more often.
6. Watch it: `tail -f cron.log`

## Configuration

- `config/permits.yaml` — which permits/divisions/date windows to watch.
- `config/config.yaml` — request rate limiting, notification channels, scheduler
  interval, and `state_file`.

Secrets (`TELEGRAM_*`, optional SMTP/Twilio) live in `.env`, never in the YAML.

## License

MIT
