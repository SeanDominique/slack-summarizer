"""Reaction-bookmarked deep research.

Pipeline:
    user's `reactions_index` → since-filter → corpus cap → cluster (Claude) →
    fan out per-theme summaries (Claude, parallel) → synthesize →
    hand off to NotebookLM wrapper → DM result.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from anthropic import Anthropic

import slack_client
from slack_client import SlackMessage
import state
from state import ReactionRecord

log = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
CLUSTER_PROMPT_PATH = Path(__file__).parent / "prompts" / "cluster_themer.md"
SUMMARIZE_PROMPT_PATH = Path(__file__).parent / "prompts" / "theme_summarizer.md"

MAX_CORPUS_MESSAGES = 200
MAX_CORPUS_TOKENS = 80_000  # rough estimate; char-based proxy below
MAX_THEMES = 6


# ---------- Corpus assembly ----------

def fetch_reaction_corpus(user_id: str, since_epoch: float) -> list[ReactionRecord]:
    """All of this user's bookmark-tagged messages since the cutoff."""
    return state.get_reactions_since(user_id, since_epoch)


def hydrate_messages(records: list[ReactionRecord]) -> list[SlackMessage]:
    """Pull full message text for each reacted message via conversations.history pagination.

    Slack has no bulk-by-ts fetch; we use conversations.replies with the exact ts which
    returns the single message (and its thread if any).
    """
    hydrated: list[SlackMessage] = []
    for rec in records:
        # conversations.replies for a non-thread ts returns just that one message.
        try:
            msgs = slack_client.fetch_thread(rec.channel, rec.ts)
            if not msgs:
                continue
            head = msgs[0]
            # Make sure permalink on the hydrated message uses the one we stored
            head.permalink = rec.permalink
            hydrated.append(head)
        except Exception as exc:
            log.warning("Failed to hydrate %s/%s: %s", rec.channel, rec.ts, exc)
            continue
    return hydrated


def cap_corpus(messages: list[SlackMessage]) -> tuple[list[SlackMessage], bool]:
    """Truncate to the cap, most-recent-first. Returns (capped_messages, was_truncated)."""
    messages = sorted(messages, key=lambda m: float(m.ts), reverse=True)

    was_truncated = False
    if len(messages) > MAX_CORPUS_MESSAGES:
        messages = messages[:MAX_CORPUS_MESSAGES]
        was_truncated = True

    # Rough token proxy: 4 chars ≈ 1 token
    total_chars = sum(len(m.text) for m in messages)
    if total_chars > MAX_CORPUS_TOKENS * 4:
        kept: list[SlackMessage] = []
        running = 0
        for m in messages:
            if running + len(m.text) > MAX_CORPUS_TOKENS * 4:
                was_truncated = True
                break
            kept.append(m)
            running += len(m.text)
        messages = kept

    return messages, was_truncated


# ---------- Prompt input shaping ----------

def render_message_block(m: SlackMessage) -> str:
    author = m.user or "unknown"
    body = m.text.replace("</slack_message>", "</slack_message_escaped>")
    return (
        f'<slack_message id="{m.ts}" author="{author}" channel="{m.channel}" '
        f'ts="{m.ts}" permalink="{m.permalink or ""}">'
        f"{body}"
        f"</slack_message>"
    )


def render_corpus(messages: list[SlackMessage]) -> str:
    return "\n".join(render_message_block(m) for m in messages)


# ---------- Cluster step ----------

def call_claude(system: str, user_content: str, max_tokens: int = 4096) -> str:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()


def strip_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def cluster_themes(messages: list[SlackMessage]) -> list[dict]:
    """Returns [{name, description, message_ids: [ts, ...]}]."""
    system = CLUSTER_PROMPT_PATH.read_text()
    user_content = (
        "Messages to cluster:\n\n" + render_corpus(messages) +
        "\n\nEmit the JSON now."
    )
    raw = call_claude(system, user_content, max_tokens=2048)
    parsed = json.loads(strip_fence(raw))
    themes = parsed.get("themes", [])
    # Validate: every message_id should appear in the input set
    valid_ids = {m.ts for m in messages}
    cleaned: list[dict] = []
    for t in themes[:MAX_THEMES]:
        ids = [i for i in t.get("message_ids", []) if i in valid_ids]
        if not ids:
            continue
        cleaned.append({
            "name": t.get("name", "Untitled theme"),
            "description": t.get("description", ""),
            "message_ids": ids,
        })
    return cleaned


# ---------- Parallel theme summarization ----------

def summarize_theme(theme: dict, messages_by_ts: dict[str, SlackMessage]) -> str:
    """Single-theme summary. Returns the markdown section."""
    system_template = SUMMARIZE_PROMPT_PATH.read_text()
    theme_msgs = [messages_by_ts[ts] for ts in theme["message_ids"] if ts in messages_by_ts]
    user_content = (
        f"theme_name: {theme['name']}\n"
        f"theme_description: {theme.get('description', '')}\n\n"
        "Messages:\n\n" + render_corpus(theme_msgs) +
        "\n\nWrite the markdown section now."
    )
    return call_claude(system_template, user_content, max_tokens=2048)


def summarize_themes_parallel(
    themes: list[dict], messages: list[SlackMessage], max_workers: int = 6
) -> list[str]:
    messages_by_ts = {m.ts: m for m in messages}
    out: list[str | None] = [None] * len(themes)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(summarize_theme, theme, messages_by_ts): idx
            for idx, theme in enumerate(themes)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                out[idx] = fut.result()
            except Exception as exc:
                log.exception("Theme %d failed: %s", idx, exc)
                out[idx] = (
                    f"## {themes[idx]['name']}\n\n"
                    f"_Summary generation failed: {exc}_\n"
                )
    return [s or "" for s in out]


# ---------- Synthesis ----------

SYNTHESIS_SYSTEM = """You are merging per-theme summaries into a single cohesive
research document that will be passed to NotebookLM to generate an audio podcast.

## Rules
- Preserve the individual `## Theme` sections verbatim — do not rewrite them.
- Add a short intro paragraph (3-4 sentences) naming the overall subject matter.
- Add a short closing paragraph (2-3 sentences) noting what's still open.
- Keep the document to 1500-3000 words total. If inputs exceed this, trust them and
  pass through — don't prune the summaries.
- No meta-commentary.

Return the full markdown document, nothing else."""


def synthesize_document(
    theme_summaries: list[str],
    user_name: str,
    since_date: str,
    was_truncated: bool,
) -> str:
    body = "\n\n".join(s for s in theme_summaries if s.strip())
    user_content = (
        f"User: {user_name}\n"
        f"Period: bookmarked messages since {since_date}\n"
        f"Truncated to corpus cap: {was_truncated}\n\n"
        f"Theme summaries:\n\n{body}"
    )
    return call_claude(SYNTHESIS_SYSTEM, user_content, max_tokens=8192)


# ---------- Orchestration ----------

def run_research(user_id: str, since_epoch: float, since_date_str: str) -> dict:
    """Returns structured result; caller handles NotebookLM + DM delivery."""
    records = fetch_reaction_corpus(user_id, since_epoch)
    if not records:
        return {"user_id": user_id, "status": "empty_corpus"}

    raw_messages = hydrate_messages(records)
    if not raw_messages:
        return {"user_id": user_id, "status": "hydration_failed"}

    capped, was_truncated = cap_corpus(raw_messages)

    user_info = slack_client.get_user_info(user_id)
    user_name = user_info.get("real_name") or user_info.get("name") or user_id

    themes = cluster_themes(capped)
    if not themes:
        return {"user_id": user_id, "status": "no_themes"}

    theme_summaries = summarize_themes_parallel(themes, capped)
    document_markdown = synthesize_document(
        theme_summaries, user_name, since_date_str, was_truncated
    )

    return {
        "user_id": user_id,
        "user_name": user_name,
        "status": "ok",
        "document_markdown": document_markdown,
        "theme_count": len(themes),
        "corpus_size": len(capped),
        "was_truncated": was_truncated,
    }
