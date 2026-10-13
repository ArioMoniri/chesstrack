# chesstrack

Watches [tsf.org.tr](https://www.tsf.org.tr) (Türkiye Satranç Federasyonu) and reports every change
to a Telegram bot, so venue and hotel announcements for championships are seen in time.
Runs on GitHub Actions, no server needed.

## Setup (works from a phone)

1. In Telegram open **@BotFather**, send `/newbot`, copy the token.
2. On GitHub: repo → **Settings → Secrets and variables → Actions → New repository secret**,
   name `TELEGRAM_BOT_TOKEN`, value: the token.
3. Send your bot any message. Groups work too: add the bot and send a message there.
4. **Actions → Watch tsf.org.tr → Run workflow**, or wait for the schedule.

## Bot commands

Type `/` in the chat to see them with autocomplete.

| Command | What it does |
|---|---|
| `/status` | mode, last check time, pages watched, changes counted, failures, link to Actions |
| `/last [n]` | the last n changes: time, page, link, new links, text diff |
| `/pages` | every watched page with its last check time |
| `/search <text>` | search the current page snapshots, e.g. `/search otel` |
| `/pause` / `/resume` | mute or unmute alerts; checks keep running |
| `/stop` | disables the GitHub workflow and cancels queued runs: no more runs, no more minutes |
| `/help` | list commands |

After `/stop`, start again on GitHub: Actions → Watch tsf.org.tr → Enable workflow → Run workflow.

## What is watched

The pages in `watcher/config.json` (home, announcements, news, calendar, tournament hub, every
championship page) plus pages discovered automatically: league hubs under `/organizasyon/`,
championship pages under `/kaynaklar/sampiyonalar/`, and event detail pages under `/etkinlik/`
for the current and next season. Finished events drop off by themselves. About 60 pages in total.

Every change is reported. Changes mentioning hotels, accommodation, venue ("Yer açıklanacak"),
dates, registration or Antalya-area resorts get an `IMPORTANT` header.

## How close to realtime

* tsf.org.tr is a Next.js site cached for 5 minutes server-side (`x-nextjs-stale-time: 300`),
  so no watcher can see a change sooner than about 5 minutes after it is published.
* Public repo: each 5-minute scheduled run loops for ~9.5 minutes checking every 30 s, using
  ETag conditional requests (cheap 304 responses when nothing changed). Effective delay:
  the site's cache plus up to ~40 s.
* Private repo: one check per scheduled run, roughly every 5–10 minutes.

## Cost

GitHub-hosted runners are free without limit on **public** repos. On a **private** repo they are
billed per minute once the plan's free minutes are used (Linux: about $0.008/min):

| Mode | Minutes / month | Cost / month (private) |
|---|---|---|
| one check per 5-minute run | ~8,600 | ~$70 |
| continuous loop | ~43,000 | ~$345 |
| any mode, public repo | — | $0 |

The workflow only enables the continuous loop when the repo is public.

## Notes

* Page snapshots are committed to `state/` only when a page actually changes. The subscriber
  list and per-check timestamps live in the Actions cache, not in git.
* GitHub disables scheduled workflows after 60 days without repo activity; the daily heartbeat
  commit keeps it alive.
