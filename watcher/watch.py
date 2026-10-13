#!/usr/bin/env python3
"""Watch pages on tsf.org.tr, report changes to Telegram, answer bot commands.

Stdlib only so it runs fast on a GitHub Actions runner.

State in ./state (pages/meta/changes are committed by the workflow, chats
lives in the Actions cache):
  pages.json    normalized text, links and ETag per page
  meta.json     counters, discovered pages, paused flag
  changes.json  the most recent changes (for /last)
  chats.json    subscribed Telegram chats and the getUpdates offset (cache)
  runtime.json  last check times and failure counter (cache, changes every check)

Exit codes: 0 ok, 1 every page failed, 3 /stop was requested.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_summary  # noqa: E402

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Istanbul")
except Exception:  # noqa: BLE001
    TZ = UTC

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CONFIG = json.loads((ROOT / "watcher" / "config.json").read_text(encoding="utf-8"))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_OVERRIDE = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
GH_WORKFLOW = os.environ.get("GITHUB_WORKFLOW_FILE", "watch.yml")
GH_RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "0") or 0)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "0") or 0)
TEST_HOST = os.environ.get("WATCH_TEST_HOST", "tsf.org.tr")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
EXIT_STOP = 3

COMMANDS = [
    ("status", "Watcher status: mode, last check, pages, changes"),
    ("last", "Last changes, e.g. /last 10"),
    ("pages", "List the watched pages"),
    ("search", "Search current page text, e.g. /search otel"),
    ("pause", "Mute alerts (checks keep running)"),
    ("resume", "Unmute alerts"),
    ("stop", "Stop the watcher and all GitHub Actions spending"),
    ("help", "Show commands"),
]


# ----------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def now_utc():
    return datetime.now(UTC)


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d %b %Y %H:%M:%S")
    except ValueError:
        return iso


def load(name, default):
    p = STATE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save(name, data):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def http(url, data=None, headers=None, timeout=30, method=None):
    h = {
        "User-Agent": UA,
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ctype = r.headers.get_content_charset() or "utf-8"
        return raw.decode(ctype, errors="replace"), dict(r.headers)


def fetch(url, etag=None, tries=3):
    """Return (html, etag) or (None, etag) when the server says 304."""
    last = None
    for i in range(tries):
        try:
            headers = {"If-None-Match": etag} if etag else {}
            body, hdrs = http(url, headers=headers)
            return body, hdrs.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, etag
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


# ----------------------------------------------------------------- parsing
class Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {
        "p",
        "div",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "section",
        "article",
        "header",
        "footer",
        "ul",
        "ol",
        "table",
        "nav",
    }

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.skip_depth = 0
        self.parts: list[str] = []
        self.links: dict[str, str] = {}
        self._a_href = None
        self._a_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._a_href = urllib.parse.urljoin(self.base, href.strip())
            self._a_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._a_href is not None:
            text = " ".join("".join(self._a_text).split())
            href = self._a_href
            if (
                text
                and len(text) >= CONFIG["min_link_text_len"]
                and href.startswith("http")
                and TEST_HOST in href
            ):
                self.links.setdefault(href.split("#")[0], text)
            self._a_href = None
            self._a_text = []

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.parts.append(data)
        if self._a_href is not None:
            self._a_text.append(data)


def normalize(raw_html, base):
    ex = Extractor(base)
    ex.feed(raw_html)
    ignore = [re.compile(p) for p in CONFIG["ignore_line_patterns"]]
    lines = []
    for line in "".join(ex.parts).splitlines():
        line = " ".join(line.split())
        if line and not any(p.search(line) for p in ignore):
            lines.append(line)
    return lines, ex.links


# ----------------------------------------------------------------- telegram
def tg(method, **params):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        body, _ = http(url, data=data)
        return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"telegram {method} -> {e.code}: {body[:300]}") from None


def send(chat_id, text):
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, 4000)
        cut = cut if cut > 1000 else 4000
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    for chunk in chunks:
        tg(
            "sendMessage",
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview="true",
        )


def send_all(chats: dict, text: str):
    if not chats:
        log("no chats subscribed yet; message dropped:\n", text[:400])
        return
    for cid in list(chats):
        try:
            send(cid, text)
        except Exception as e:  # noqa: BLE001
            log(f"send to {cid} failed:", e)
            if "chat not found" in str(e) or "bot was blocked" in str(e):
                chats.pop(cid, None)


def register_commands():
    try:
        tg(
            "setMyCommands",
            commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDS]),
        )
    except Exception as e:  # noqa: BLE001
        log("setMyCommands failed:", e)


# ----------------------------------------------------------------- github
def gh(method, path, body=None):
    if not (GH_TOKEN and GH_REPO):
        raise RuntimeError("GITHUB_TOKEN / GITHUB_REPOSITORY not available")
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    try:
        text, _ = http(
            f"https://api.github.com/repos/{GH_REPO}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"github {method} {path} -> {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
        ) from None


def stop_workflow():
    """Disable the workflow so no further runs (or minutes) happen."""
    gh("PUT", f"/actions/workflows/{GH_WORKFLOW}/disable")
    cancelled = 0
    for status in ("queued", "pending", "waiting"):
        try:
            runs = gh("GET", f"/actions/runs?status={status}&per_page=50").get("workflow_runs", [])
        except Exception as e:  # noqa: BLE001
            log("list runs failed:", e)
            continue
        for run in runs:
            if str(run.get("id")) == GH_RUN_ID:
                continue
            try:
                gh("POST", f"/actions/runs/{run['id']}/cancel")
                cancelled += 1
            except Exception as e:  # noqa: BLE001
                log("cancel failed:", e)
    return cancelled


# ----------------------------------------------------------------- commands
def actions_url():
    return f"https://github.com/{GH_REPO}/actions" if GH_REPO else ""


def cmd_help():
    lines = ["<b>ChessTrack</b> watches tsf.org.tr and reports every change here.\n"]
    for c, d in COMMANDS:
        lines.append(f"/{c} – {esc(d)}")
    return "\n".join(lines)


def cmd_status(meta, rt, pages, chats):
    if LOOP_SECONDS > 0:
        mode = f"continuous, a check every ~{POLL_SECONDS + 8}s (public repo)"
    else:
        mode = "one check per scheduled run, every 5-10 min (private repo)"
    last = rt.get("last_check")
    ago = ""
    if last:
        secs = int((now_utc() - datetime.fromisoformat(last)).total_seconds())
        ago = f" ({secs}s ago)"
    rows = [
        "<b>ChessTrack status</b>",
        f"Alerts: {'⏸ paused' if meta.get('paused') else '🟢 active'}",
        f"Mode: {esc(mode)}",
        f"Last check: {esc(fmt_time(last))}{ago}",
        f"Pages watched: {len(pages)}",
        f"Changes detected: {meta.get('total_changes', 0)} "
        f"(last: {esc(fmt_time(meta.get('last_change_at')))})",
        f"Consecutive fetch failures: {rt.get('failures', 0)}",
        f"Subscribers: {len(chats)}",
        f"Watching since: {esc(fmt_time(meta.get('started_at')))}",
    ]
    if actions_url():
        rows.append(f'<a href="{actions_url()}">GitHub Actions runs</a>')
    rows.append(f"AI summaries: {'on' if ai_summary.available() else 'off (no ANTHROPIC_API_KEY)'}")
    rows.append("Site note: tsf.org.tr caches pages for ~5 min, so that is the floor.")
    return "\n".join(rows)


def cmd_last(changes, arg):
    try:
        n = max(1, min(20, int(arg)))
    except (TypeError, ValueError):
        n = 5
    if not changes:
        return "No changes recorded yet."
    out = [f"<b>Last {min(n, len(changes))} change(s)</b>"]
    for ch in list(reversed(changes))[:n]:
        hot = " 🏨" if ch.get("hot") else ""
        out.append(
            f"\n🕒 {esc(fmt_time(ch['time']))}{hot}\n"
            f'📄 <a href="{esc(ch["url"])}">{esc(ch["page"])}</a>'
        )
        if ch.get("ai"):
            out.append(f"🤖 {esc(ch['ai'])}")
        for u, t in ch.get("new_links", [])[:5]:
            out.append(f'  • <a href="{esc(u)}">{esc(t)}</a>')
        snip = ch.get("snippet", "")
        if snip:
            out.append(esc(snip[:400]) + ("…" if len(snip) > 400 else ""))
    return "\n".join(out)


def cmd_pages(pages, rt):
    out = [f"<b>{len(pages)} pages watched</b>"]
    for p in pages:
        chk = rt.get("checked", {}).get(p["url"])
        chk = fmt_time(chk) if chk else "pending"
        out.append(f'• <a href="{esc(p["url"])}">{esc(p["name"][:60])}</a> – {esc(chk)}')
    return "\n".join(out)


def cmd_search(pages, pages_state, query):
    q = (query or "").strip().casefold()
    if len(q) < 2:
        return "Usage: /search <text>  (e.g. /search otel)"
    hits = []
    names = {p["url"]: p["name"] for p in pages}
    for url, st in pages_state.items():
        for line in st.get("lines", []):
            if q in line.casefold():
                hits.append(
                    f'• <a href="{esc(url)}">{esc(names.get(url, url)[:40])}</a>: {esc(line[:160])}'
                )
                if len(hits) >= 25:
                    break
        if len(hits) >= 25:
            break
    if not hits:
        return f"No match for “{esc(query)}” in the current snapshots."
    return f"<b>Matches for “{esc(query)}”</b>\n" + "\n".join(hits)


def handle_updates(chats_state, meta, rt, pages, pages_state, changes):
    """Register new chats and answer commands. Returns True when /stop was requested."""
    chats = chats_state["chats"]
    stop = False
    try:
        res = tg(
            "getUpdates",
            offset=chats_state.get("offset", 0),
            timeout=0,
            allowed_updates=json.dumps(["message", "channel_post"]),
        )
    except Exception as e:  # noqa: BLE001
        log("getUpdates failed:", e)
        return False
    for upd in res.get("result", []):
        chats_state["offset"] = max(chats_state.get("offset", 0), upd["update_id"] + 1)
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        cid = str(cid)
        title = (
            chat.get("title")
            or " ".join(x for x in [chat.get("first_name"), chat.get("last_name")] if x)
            or chat.get("username")
            or ""
        )
        new = cid not in chats
        if new:
            chats[cid] = title
            log(f"new subscriber chat {cid} ({title})")
        text = (msg.get("text") or "").strip()
        reply = None
        if new and not text.startswith("/"):
            reply = "✅ Connected. " + cmd_help()
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            if cmd in ("start", "help"):
                reply = ("✅ Connected. " if new else "") + cmd_help()
            elif cmd == "status":
                reply = cmd_status(meta, rt, pages, chats)
            elif cmd == "last":
                reply = cmd_last(changes, arg)
            elif cmd == "pages":
                reply = cmd_pages(pages, rt)
            elif cmd == "search":
                reply = cmd_search(pages, pages_state, arg)
            elif cmd == "pause":
                meta["paused"] = True
                reply = "⏸ Alerts paused. Checks keep running; /resume to unmute."
            elif cmd == "resume":
                meta["paused"] = False
                reply = "▶️ Alerts resumed."
            elif cmd == "stop":
                stop = True
                reply = (
                    "🛑 Stopping. The GitHub workflow is being disabled, so no more runs "
                    "and no more minutes are spent.\nTo start again: GitHub → Actions → "
                    "“Watch tsf.org.tr” → Enable workflow → Run workflow."
                    + (f"\n{actions_url()}" if actions_url() else "")
                )
            else:
                reply = "Unknown command.\n" + cmd_help()
        if reply:
            try:
                send(cid, reply)
            except Exception as e:  # noqa: BLE001
                log(f"reply to {cid} failed:", e)
    return stop


# ----------------------------------------------------------------- discovery
TR_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "ocak",
            "şubat",
            "mart",
            "nisan",
            "mayıs",
            "haziran",
            "temmuz",
            "ağustos",
            "eylül",
            "ekim",
            "kasım",
            "aralık",
        ]
    )
}
DATE_RX = re.compile(r"(\d{1,2})\s+([A-Za-zÇçĞğİıÖöŞşÜü]+)\s+(20\d{2})")


def event_finished(text, grace_days=7):
    """True when the last date mentioned in text is clearly in the past."""
    dates = []
    for d, m, y in DATE_RX.findall(text):
        mi = TR_MONTHS.get(m.casefold())
        if mi:
            with contextlib.suppress(ValueError):
                dates.append(datetime(int(y), mi, int(d), tzinfo=UTC))
    return bool(dates) and (now_utc() - max(dates)).days > grace_days


def discover(pages_state, known_urls):
    """Add links under discover_prefixes / discover_rules seen on watched pages."""
    y = now_utc().year
    rules = [(p, None, None) for p in CONFIG.get("discover_prefixes", [])]
    rules += [
        (
            r["prefix"],
            re.compile(r["text_regex"].replace("{Y}", str(y)).replace("{Y1}", str(y + 1))),
            re.compile(r["exclude_regex"]) if r.get("exclude_regex") else None,
        )
        for r in CONFIG.get("discover_rules", [])
    ]
    found = {}
    for st in pages_state.values():
        for u, t in st.get("links", {}).items():
            u = u.split("?")[0].rstrip("/") or u
            if u in known_urls or u in found:
                continue
            for prefix, rx, ex in rules:
                if u.startswith(prefix) and (rx is None or rx.search(t)):
                    if rx is not None and (event_finished(t) or (ex and ex.search(t))):
                        break
                    found[u] = t[:80]
                    break
    return [{"name": t, "url": u} for u, t in found.items()]


# ----------------------------------------------------------------- checking
def diff_snippet(old_lines, new_lines, limit):
    out = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(("➕ " if line.startswith("+") else "➖ ") + line[1:].strip())
    text = "\n".join(out)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…"
    return text


def check_page(page, prev):
    raw, etag = fetch(page["url"], etag=(prev or {}).get("etag"))
    checked = now_utc().isoformat(timespec="seconds")
    if raw is None:  # 304 Not Modified
        return prev, None
    lines, links = normalize(raw, page["url"])
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    cur = {"hash": digest, "lines": lines, "links": links, "etag": etag}
    if prev is None or prev["hash"] == digest:
        return cur, None

    new_links = [(u, t) for u, t in links.items() if u not in prev.get("links", {})]
    snippet = diff_snippet(prev.get("lines", []), lines, CONFIG["max_diff_chars"])
    probe = (snippet + " " + " ".join(t for _, t in new_links)).casefold()
    hot = [k for k in CONFIG.get("highlight_keywords", []) if k.casefold() in probe]
    ai = ai_summary.summarize(page["name"], page["url"], snippet, new_links)
    if ai and (ai.get("importance") == "high" or ai.get("venue_or_hotel")):
        hot = hot or ["ai"]
    head = "🏨🚨 <b>IMPORTANT</b> (" + esc(", ".join(hot)) + ")\n" if hot else ""
    if ai:
        head += f"🤖 {esc(ai['summary'])}\n" + (
            f"👉 {esc(ai['action'])}\n" if ai.get("action", "nothing") != "nothing" else ""
        )
    msg = [
        f"{head}🔔 <b>{esc(page['name'])}</b> changed\n"
        f'<a href="{esc(page["url"])}">{esc(page["url"])}</a>\n'
        f"🕒 {esc(fmt_time(checked))}"
    ]
    if new_links:
        msg.append("\n<b>New links:</b>")
        for u, t in new_links[:15]:
            msg.append(f'• <a href="{esc(u)}">{esc(t)}</a>')
        if len(new_links) > 15:
            msg.append(f"… and {len(new_links) - 15} more")
    if snippet:
        msg.append("\n<b>Text diff:</b>\n" + esc(snippet))
    record = {
        "time": checked,
        "page": page["name"],
        "url": page["url"],
        "new_links": new_links[:15],
        "snippet": snippet,
        "hot": hot,
        "ai": ai.get("summary") if ai else None,
    }
    return cur, ("\n".join(msg), record)


def main():
    pages_state = load("pages.json", {})
    meta = load("meta.json", {})
    meta.pop("failures", None)
    meta.pop("last_check", None)
    meta.setdefault("discovered", [])
    rt = load("runtime.json", {})
    rt.setdefault("failures", 0)
    rt.setdefault("checked", {})
    meta.setdefault("total_changes", 0)
    meta.setdefault("started_at", now_utc().isoformat(timespec="seconds"))
    changes = load("changes.json", [])
    chats_state = load("chats.json", {})
    if "chats" not in chats_state:  # migrate the old flat {id: name} layout
        chats_state = {
            "chats": {k: v for k, v in chats_state.items() if k != "offset"},
            "offset": 0,
        }
    chats = chats_state["chats"]
    if CHAT_ID_OVERRIDE:
        chats.setdefault(CHAT_ID_OVERRIDE, "override")

    try:
        me = tg("getMe").get("result", {})
        log(f"bot: @{me.get('username')} (id {me.get('id')})")
    except Exception as e:  # noqa: BLE001
        log("getMe failed:", e)
    register_commands()

    # page list: config + discovered (minus finished events)
    pages = list(CONFIG["pages"])
    known = {p["url"].rstrip("/") or p["url"] for p in pages}
    keep = []
    for d in meta["discovered"]:
        if event_finished(d["name"], grace_days=14):
            log("dropping finished event:", d["url"], "-", d["name"][:60])
            continue
        keep.append(d)
        if d["url"] not in known:
            pages.append(d)
            known.add(d["url"])
    meta["discovered"] = keep
    for d in discover(pages_state, known):
        if len(pages) >= CONFIG.get("discover_max", 40):
            break
        log("discovered page:", d["url"], "-", d["name"])
        pages.append(d)
        known.add(d["url"])
        meta["discovered"].append(d)
    watched = {p["url"] for p in pages}
    for stale in [u for u in pages_state if u not in watched]:
        log("pruning stale state for", stale)
        pages_state.pop(stale, None)

    stop = handle_updates(chats_state, meta, rt, pages, pages_state, changes)
    if stop:
        save("meta.json", meta)
        save("chats.json", chats_state)
        try:
            n = stop_workflow()
            log(f"workflow disabled, {n} queued run(s) cancelled")
            send_all(chats, "✅ Workflow disabled. ChessTrack is stopped.")
        except Exception as e:  # noqa: BLE001
            log("stop_workflow failed:", e)
            send_all(
                chats,
                "⚠️ Could not disable the workflow automatically: "
                + esc(str(e))
                + "\nDisable it on GitHub → Actions → Watch tsf.org.tr → ⋯ → Disable workflow.",
            )
        sys.exit(EXIT_STOP)

    first_run = not pages_state
    messages, errors = [], []

    def work(page):
        try:
            return page, check_page(page, pages_state.get(page["url"])), None
        except Exception as e:  # noqa: BLE001
            return page, None, e

    with ThreadPoolExecutor(max_workers=CONFIG.get("workers", 4)) as ex:
        for page, result, err in ex.map(work, pages):
            if err is not None:
                errors.append(f"{page['name']}: {err}")
                log("ERROR", page["name"], err)
                continue
            cur, hit = result
            pages_state[page["url"]] = cur
            rt["checked"][page["url"]] = now_utc().isoformat(timespec="seconds")
            if hit:
                messages.append(hit)

    for text, record in messages:
        changes.append(record)
        meta["total_changes"] += 1
        meta["last_change_at"] = record["time"]
        if meta.get("paused"):
            log("paused; alert suppressed for", record["page"])
        else:
            send_all(chats, text)
    del changes[:-100]

    if first_run and not errors:
        send_all(
            chats,
            "🟢 <b>ChessTrack</b> started watching.\n"
            + "\n".join(f"• {esc(p['name'])}" for p in pages),
        )

    if errors:
        rt["failures"] += 1
        if rt["failures"] == CONFIG["failure_alert_after"]:
            send_all(
                chats,
                f"⚠️ <b>ChessTrack</b> could not reach the site on "
                f"{rt['failures']} consecutive checks:\n" + esc("\n".join(errors)),
            )
    else:
        if rt["failures"] >= CONFIG["failure_alert_after"]:
            send_all(chats, "✅ <b>ChessTrack</b> can reach the site again.")
        rt["failures"] = 0

    rt["last_check"] = now_utc().isoformat(timespec="seconds")
    rt["checked"] = {u: t for u, t in rt["checked"].items() if u in watched}
    meta["heartbeat"] = now_utc().strftime("%Y-%m-%d")
    save("pages.json", pages_state)
    save("changes.json", changes)
    save("chats.json", chats_state)
    save("runtime.json", rt)
    save("meta.json", meta)
    log(
        f"done: {len(pages)} page(s), {len(messages)} change(s), {len(errors)} error(s), "
        f"{len(chats)} chat(s)"
    )
    if errors and len(errors) == len(pages):
        sys.exit(1)


if __name__ == "__main__":
    main()
