"""Optional AI summary of a detected change, via the official Anthropic SDK.

Activates only when ANTHROPIC_API_KEY is set and the `anthropic` package is
installed; otherwise summarize() returns None and the watcher sends the raw diff.
"""

from __future__ import annotations

import json
import os

MODEL = os.environ.get("CHESSTRACK_AI_MODEL", "claude-opus-5-5")

SYSTEM = (
    "You summarize changes detected on tsf.org.tr, the Turkish Chess Federation website, "
    "for a chess player who needs to book a hotel the moment a championship venue is announced. "
    "You receive the page name, URL, newly added links and a unified text diff "
    "(➕ added, ➖ removed), "
    "mostly in Turkish. Answer in English, concise and factual. Never invent details that are not "
    "in the diff."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One or two sentences: what changed."},
        "importance": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "high = a venue, hotel, date or registration detail for a "
            "championship was announced or changed.",
        },
        "venue_or_hotel": {
            "type": "boolean",
            "description": "True if a venue, city or hotel is named or changed.",
        },
        "action": {"type": "string", "description": "What the reader should do now, or 'nothing'."},
    },
    "required": ["summary", "importance", "venue_or_hotel", "action"],
    "additionalProperties": False,
}


def available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def summarize(page_name: str, url: str, snippet: str, new_links: list) -> dict | None:
    if not available():
        return None
    import anthropic

    client = anthropic.Anthropic(max_retries=2, timeout=60.0)
    links = "\n".join(f"- {t} <{u}>" for u, t in new_links[:15]) or "(none)"
    user = (
        f"Page: {page_name}\nURL: {url}\n\nNew links:\n{links}\n\n"
        f"Diff:\n{snippet[:6000] or '(no text diff, links only)'}"
    )
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError:
        return None
    except anthropic.APIStatusError as e:
        print(f"ai summary: API error {e.status_code}: {e.message}", flush=True)
        return None
    except anthropic.APIConnectionError as e:
        print(f"ai summary: connection error: {e}", flush=True)
        return None
    if response.stop_reason == "refusal":
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and "summary" in data else None
