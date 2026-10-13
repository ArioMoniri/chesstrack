#!/usr/bin/env python3
"""Watch pages on tsf.org.tr and report changes to a Telegram bot.

Stdlib only so it runs fast on a GitHub Actions cron.

State lives in ./state (committed back to the repo by the workflow):
  state/pages.json   normalized text + known links per page
  state/chats.json   Telegram chat ids that have talked to the bot
  state/meta.json    consecutive failure count, heartbeat date
"""
from __future__ import annotations

import difflib
from concurrent.futures import ThreadPoolExecutor
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
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CONFIG = json.loads((ROOT / "watcher" / "config.json").read_text(encoding="utf-8"))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_OVERRIDE = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TEST_HOST = os.environ.get("WATCH_TEST_HOST", "tsf.org.tr")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ----------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


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
    (STATE / name).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                              encoding="utf-8")


def http(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ctype = r.headers.get_content_charset() or "utf-8"
        return raw.decode(ctype, errors="replace")


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            return http(url)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


# ----------------------------------------------------------------- parsing
class Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
             "br", "section", "article", "header", "footer", "ul", "ol", "table", "nav"}

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
            href = urllib.parse.urljoin(self.base, href.strip())
            self._a_href = href
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
            if (text and len(text) >= CONFIG["min_link_text_len"]
                    and href.startswith("http") and (TEST_HOST in href or "tsf.org.tr" in href)
                    and not href.lower().startswith(("javascript:", "mailto:"))):
                clean = href.split("#")[0]
                self.links.setdefault(clean, text)
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
    text = "".join(ex.parts)
    ignore = [re.compile(p) for p in CONFIG["ignore_line_patterns"]]
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line or any(p.search(line) for p in ignore):
            continue
        lines.append(line)
    return lines, ex.links


# ----------------------------------------------------------------- telegram
def tg(method, **params):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        return json.loads(http(url, data=data))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"telegram {method} -> {e.code}: {body[:300]}") from None


def discover_chats(known: dict) -> dict:
    """Any chat that has sent the bot a message gets subscribed."""
    try:
        me = tg("getMe").get("result", {})
        log(f"bot: @{me.get('username')} (id {me.get('id')})")
    except Exception as e:  # noqa: BLE001
        log("getMe failed:", e)
    try:
        res = tg("getUpdates", timeout=0, allowed_updates=json.dumps(["message", "channel_post"]))
    except Exception as e:  # noqa: BLE001
        log("getUpdates failed:", e)
        return known
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        title = chat.get("title") or " ".join(
            x for x in [chat.get("first_name"), chat.get("last_name")] if x) or chat.get("username") or ""
        if str(cid) not in known:
            known[str(cid)] = title
            log(f"new subscriber chat {cid} ({title})")
            try:
                tg("sendMessage", chat_id=cid, parse_mode="HTML",
                   text="✅ <b>ChessTrack</b> bu sohbete bağlandı.\n"
                        "tsf.org.tr üzerinde bir değişiklik olduğunda buraya yazacağım.")
            except Exception as e:  # noqa: BLE001
                log("welcome failed:", e)
    return known


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def send_all(chats: dict, text: str):
    if not chats:
        log("no chats subscribed yet; message dropped:\n", text[:400])
        return
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, 4000)
        cut = cut if cut > 1000 else 4000
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    for cid in list(chats):
        for chunk in chunks:
            try:
                tg("sendMessage", chat_id=cid, text=chunk, parse_mode="HTML",
                   disable_web_page_preview="true")
            except Exception as e:  # noqa: BLE001
                log(f"send to {cid} failed:", e)
                if "chat not found" in str(e) or "bot was blocked" in str(e):
                    chats.pop(cid, None)


# ----------------------------------------------------------------- main
def diff_snippet(old_lines, new_lines, limit):
    out = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        mark = "➕" if line.startswith("+") else "➖"
        out.append(f"{mark} {esc(line[1:].strip())}")
    text = "\n".join(out)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…"
    return text


def check_page(page, prev):
    raw = fetch(page["url"])
    lines, links = normalize(raw, page["url"])
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    cur = {"hash": digest, "lines": lines, "links": links,
           "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if prev is None:
        return cur, None  # baseline
    if prev["hash"] == digest:
        return cur, None

    new_links = [(u, t) for u, t in links.items() if u not in prev.get("links", {})]
    snippet = diff_snippet(prev.get("lines", []), lines, CONFIG["max_diff_chars"])
    probe = (snippet + " " + " ".join(t for _, t in new_links)).casefold()
    hot = [k for k in CONFIG.get("highlight_keywords", []) if k.casefold() in probe]
    head = "🏨🚨 <b>ÖNEMLİ</b> (" + esc(", ".join(hot)) + ")\n" if hot else ""
    msg = [f"{head}🔔 <b>{esc(page['name'])}</b> değişti\n<a href=\"{esc(page['url'])}\">{esc(page['url'])}</a>"]
    if new_links:
        msg.append("\n<b>Yeni bağlantılar:</b>")
        for u, t in new_links[:15]:
            msg.append(f"• <a href=\"{esc(u)}\">{esc(t)}</a>")
        if len(new_links) > 15:
            msg.append(f"… ve {len(new_links) - 15} tane daha")
    if snippet:
        msg.append("\n<b>Metin farkı:</b>\n" + snippet)
    return cur, "\n".join(msg)


TR_MONTHS = {m: i + 1 for i, m in enumerate(
    ["ocak", "şubat", "mart", "nisan", "mayıs", "haziran", "temmuz", "ağustos",
     "eylül", "ekim", "kasım", "aralık"])}
DATE_RX = re.compile(r"(\d{1,2})\s+([A-Za-zÇçĞğİıÖöŞşÜü]+)\s+(20\d{2})")


def event_finished(text, grace_days=7):
    """True when the last date mentioned in text is clearly in the past."""
    dates = []
    for d, m, y in DATE_RX.findall(text):
        mi = TR_MONTHS.get(m.casefold())
        if mi:
            try:
                dates.append(datetime(int(y), mi, int(d), tzinfo=timezone.utc))
            except ValueError:
                pass
    if not dates:
        return False
    return (datetime.now(timezone.utc) - max(dates)).days > grace_days


def discover(pages_state, known_urls):
    """Add any link under a discover_prefix seen on a watched page."""
    rules = [(p, None) for p in CONFIG.get("discover_prefixes", [])]
    y = datetime.now(timezone.utc).year
    rules += [(r["prefix"], re.compile(r["text_regex"].replace("{Y}", str(y)).replace("{Y1}", str(y + 1))))
              for r in CONFIG.get("discover_rules", [])]
    if not rules:
        return []
    found = {}
    for st in pages_state.values():
        for u, t in st.get("links", {}).items():
            u = u.split("?")[0].rstrip("/") or u
            if u in known_urls or u in found:
                continue
            for prefix, rx in rules:
                if u.startswith(prefix) and (rx is None or rx.search(t)):
                    if rx is not None and event_finished(t):
                        break
                    found[u] = t[:80]
                    break
    return [{"name": t, "url": u} for u, t in found.items()]


def main():
    pages_state = load("pages.json", {})
    chats = load("chats.json", {})
    meta = load("meta.json", {"failures": 0, "heartbeat": "", "discovered": []})

    if CHAT_ID_OVERRIDE:
        chats.setdefault(CHAT_ID_OVERRIDE, "override")
    chats = discover_chats(chats)

    pages = list(CONFIG["pages"])
    known = {p["url"].rstrip("/") or p["url"] for p in pages}
    for d in meta.get("discovered", []):
        if d["url"] not in known:
            pages.append(d)
            known.add(d["url"])
    for d in discover(pages_state, known):
        if len(pages) >= CONFIG.get("discover_max", 40):
            break
        log("discovered page:", d["url"], "-", d["name"])
        pages.append(d)
        known.add(d["url"])
        meta.setdefault("discovered", []).append(d)

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
            cur, msg = result
            pages_state[page["url"]] = cur
            if msg:
                messages.append(msg)

    for m in messages:
        send_all(chats, m)

    if first_run and not errors:
        send_all(chats, "🟢 <b>ChessTrack</b> izlemeye başladı.\nİzlenen sayfalar:\n" +
                 "\n".join(f"• {esc(p['name'])}" for p in pages))

    if errors:
        meta["failures"] = meta.get("failures", 0) + 1
        if meta["failures"] == CONFIG["failure_alert_after"]:
            send_all(chats, "⚠️ <b>ChessTrack</b> üst üste " + str(meta["failures"]) +
                     " kontrolde siteye ulaşamadı:\n" + esc("\n".join(errors)))
    else:
        if meta.get("failures", 0) >= CONFIG["failure_alert_after"]:
            send_all(chats, "✅ <b>ChessTrack</b> siteye tekrar ulaşıyor.")
        meta["failures"] = 0

    meta["heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save("pages.json", pages_state)
    save("chats.json", chats)
    save("meta.json", meta)
    log(f"done: {len(pages)} page(s), {len(messages)} change(s), {len(errors)} error(s), {len(chats)} chat(s)")
    if errors and len(errors) == len(pages):
        sys.exit(1)


if __name__ == "__main__":
    main()
