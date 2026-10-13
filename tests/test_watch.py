"""End-to-end tests for the watcher against a local fixture server, Telegram and GitHub stubbed."""

from __future__ import annotations

import http.server
import importlib.util
import json
import shutil
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PAGE1 = (
    '<html><head><script>x</script></head><body><nav><a href="/duyurular">Duyurular</a></nav>'
    '<div><h3><a href="/etkinlik/1067">2026 Türkiye Şampiyonası 3–14 Aralık 2026 · '
    "Yer açıklanacak</a></h3></div><footer>Ziyaretçi 12345</footer></body></html>"
)
PAGE2 = PAGE1.replace("Yer açıklanacak", "Antalya - Belek Regnum Carya Hotel").replace(
    "12345", "12399"
)


class Site:
    def __init__(self):
        self.page, self.etag = PAGE1, '"v1"'
        site = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("If-None-Match") == site.etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                body = site.page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("ETag", site.etag)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    site = Site()
    work = tmp_path / "repo"
    shutil.copytree(ROOT / "watcher", work / "watcher")
    cfg = json.loads((work / "watcher/config.json").read_text(encoding="utf-8"))
    cfg["pages"] = [{"name": "Takvim", "url": f"http://127.0.0.1:{site.port}/takvim"}]
    cfg["discover_rules"] = [
        {"prefix": f"http://127.0.0.1:{site.port}/etkinlik/", "text_regex": "{Y}|{Y1}"}
    ]
    (work / "watcher/config.json").write_text(json.dumps(cfg), encoding="utf-8")
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "dummy",
        "GITHUB_TOKEN": "x",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "1",
        "LOOP_SECONDS": "570",
        "POLL_SECONDS": "30",
        "WATCH_TEST_HOST": "127.0.0.1",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("watch_under_test", work / "watcher/watch.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)

    sent, gh_calls, pending, uid = [], [], [], [0]

    def fake_tg(method, **p):
        if method == "getUpdates":
            res = []
            for text in pending:
                uid[0] += 1
                res.append(
                    {
                        "update_id": uid[0],
                        "message": {"chat": {"id": 4242, "first_name": "Ario"}, "text": text},
                    }
                )
            pending.clear()
            return {"ok": True, "result": [r for r in res if r["update_id"] >= p.get("offset", 0)]}
        if method == "getMe":
            return {"ok": True, "result": {"username": "testbot"}}
        if method == "sendMessage":
            sent.append(p["text"])
        return {"ok": True, "result": {}}

    def fake_gh(method, path, body=None):
        gh_calls.append((method, path))
        return {"workflow_runs": [{"id": 99}]} if "runs?" in path else {}

    w.tg, w.gh = fake_tg, fake_gh

    def run(*cmds):
        pending.extend(cmds)
        sent.clear()
        try:
            w.main()
            return 0
        except SystemExit as e:
            return e.code

    return type(
        "H",
        (),
        {
            "site": site,
            "run": staticmethod(run),
            "sent": sent,
            "gh_calls": gh_calls,
            "work": work,
            "mod": w,
        },
    )


def test_baseline_then_304_then_change(harness):
    assert harness.run("/start") == 0
    assert any("Connected" in m for m in harness.sent)
    assert any("started watching" in m for m in harness.sent)

    assert harness.run() == 0
    assert harness.sent == []  # 304, no alert

    harness.site.page, harness.site.etag = PAGE2, '"v2"'
    assert harness.run() == 0
    alert = "\n".join(harness.sent)
    assert "IMPORTANT" in alert and "Belek" in alert and "➖" in alert and "➕" in alert

    rt = json.loads((harness.work / "state/runtime.json").read_text(encoding="utf-8"))
    assert rt["failures"] == 0 and rt["last_check"]
    changes = json.loads((harness.work / "state/changes.json").read_text(encoding="utf-8"))
    # the fixture server answers every path, so the discovered event page changes too
    assert {c["page"] for c in changes} >= {"Takvim"} and all(c["hot"] for c in changes)


def test_commands(harness):
    harness.run("/start")
    harness.run("/status", "/last", "/pages", "/search açıklanacak", "/nope")
    joined = "\n".join(harness.sent)
    assert "ChessTrack status" in joined and "public repo" in joined
    assert "No changes recorded yet" in joined
    assert "pages watched" in joined
    assert "Matches for" in joined and "Yer açıklanacak" in joined
    assert "Unknown command" in joined


def test_pause_suppresses_alerts(harness):
    harness.run("/start")
    harness.run("/pause")
    harness.site.page, harness.site.etag = PAGE2, '"v2"'
    harness.run()
    assert harness.sent == []
    harness.run("/resume", "/last 1")
    assert any("Belek" in m for m in harness.sent)  # still recorded while paused


def test_stop_disables_workflow(harness):
    harness.run("/start")
    assert harness.run("/stop") == harness.mod.EXIT_STOP
    assert ("PUT", "/actions/workflows/watch.yml/disable") in harness.gh_calls
    assert ("POST", "/actions/runs/99/cancel") in harness.gh_calls
    assert any("Workflow disabled" in m for m in harness.sent)


def test_discovery_adds_event_page(harness):
    harness.run("/start")
    harness.run()
    meta = json.loads((harness.work / "state/meta.json").read_text(encoding="utf-8"))
    assert any(d["url"].endswith("/etkinlik/1067") for d in meta["discovered"])


def test_event_finished_parsing(harness):
    ef = harness.mod.event_finished
    assert ef("Turnuva 1 Ocak 2020 – 5 Ocak 2020")
    assert not ef("Yer belirtilmemiş")
    assert not ef("Şampiyona 1 Ocak 2099")
