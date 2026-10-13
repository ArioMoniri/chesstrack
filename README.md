# chesstrack

Watches pages on [tsf.org.tr](https://www.tsf.org.tr) (Türkiye Satranç Federasyonu) and reports
changes to a Telegram bot. Runs on GitHub Actions every 5 minutes, no server needed.

## Setup (works from a phone)

1. In Telegram open **@BotFather**, send `/newbot`, follow the prompts, copy the token.
2. On GitHub open this repo → **Settings → Secrets and variables → Actions → New repository secret**.
   Name: `TELEGRAM_BOT_TOKEN`, value: the token. Save.
3. In Telegram open your new bot and send it any message (for example `/start`).
   Groups work too: add the bot to the group and send a message there.
4. On GitHub open **Actions → Watch tsf.org.tr → Run workflow** (or just wait up to 5 minutes).

The first run sends "ChessTrack izlemeye başladı" and stores a baseline. After that you get a
message whenever a watched page changes, with new links and a text diff.

Optional: set a `TELEGRAM_CHAT_ID` secret to force a specific chat instead of auto-discovery.

## What is watched

Edit `watcher/config.json` to add or remove pages, or to add regexes for lines that should be
ignored (counters, dates, etc.).

## Notes

* GitHub's cron minimum is 5 minutes and runs can lag during busy periods. If you ever need
  sub-minute alerts, the same `watcher/watch.py` can be run from any always-on host with
  `TELEGRAM_BOT_TOKEN` in the environment.
* State is committed to `state/` by the workflow, so history of what changed is in git.
* GitHub disables scheduled workflows after 60 days without repo activity; the daily state
  heartbeat commit keeps it alive.
