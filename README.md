# chesstrack

Watches pages on [tsf.org.tr](https://www.tsf.org.tr) (Türkiye Satranç Federasyonu) and reports
changes to a Telegram bot. Runs on GitHub Actions, no server needed. On a **public** repo it checks roughly every 40 seconds
(continuous loop inside 5-minute scheduled runs). On a private repo it checks once per scheduled
run, about every 5 to 10 minutes, because private repos get only 2000 free Actions minutes a month.

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

The pages in `watcher/config.json`: home, announcements, news, event calendar and every national
championship page. Any link found under `/kaynaklar/sampiyonalar/` on a watched page is added
automatically, so new championship pages are picked up without config changes.

Changes that mention hotels, accommodation, venue, dates, registration or regulations are flagged
`ÖNEMLİ` at the top of the message.

Edit the config to add or remove pages, keywords, or regexes for lines to ignore.

## Notes

* GitHub's scheduler can lag during busy periods; the loop covers most of that. For guaranteed
  sub-minute alerts, run `watcher/watch.py` from any always-on host with `TELEGRAM_BOT_TOKEN` set.
* Page snapshots are committed to `state/` by the workflow, so the history of what changed is in
  git. The subscriber list (`state/chats.json`) is kept in the Actions cache, not in git.
* GitHub disables scheduled workflows after 60 days without repo activity; the daily state
  heartbeat commit keeps it alive.
