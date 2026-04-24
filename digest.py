"""Daily digest for a single user.

Orchestration:
    fetch → filter → group → delimiter-wrap → Claude call → validate → render → DM
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from anthropic import Anthropic

import slack_client
from slack_client import SlackMessage, ChannelInfo
import state
from state import UserPrefs
import validate

log = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-4-5-20250929"  # update to 4.6 when GA in your account
MAX_CHANNELS_PER_USER = 50
MAX_API_CALLS_PER_DIGEST = 200
DEFAULT_WINDOW_HOURS = 24
COLD_START_MAX_HOURS = 24
PROMPT_PATH = Path(__file__).parent / "prompts" / "daily_digest.md"


# ---------- Bot / noise filtering ----------

KNOWN_INTEGRATION_HINTS = {
    "github", "linear", "zapier", "pagerduty", "datadog",
    "sentry", "vercel", "render", "circleci",
}


def is_noise(msg: SlackMessage) -> bool:
    """Filter messages that don't deserve Claude's attention."""
    if msg.subtype in {
        "bot_message", "channel_join", "channel_leave",
        "channel_topic", "channel_purpose", "channel_name",
    }:
        # bot_message could occasionally matter — but for v1 drop them all.
        return True
    if not msg.text or len(msg.text.strip()) < 4:
        return True
    # emoji-only
    if all(c in "  :+-_abcdefghijklmnopqrstuvwxyz0123456789" for c in msg.text.lower()) and len(msg.text) < 30:
        if msg.text.count(":") >= 2:
            return True
    return False


# ---------- Window logic ----------

def compute_window_hours(prefs: UserPrefs) -> int:
    """Cold-start narrows the window so the first digest isn't polluted by stale history."""
    if prefs.first_digest_sent:
        return DEFAULT_WINDOW_HOURS
    # First digest: bound by opt-in time
    if prefs.opted_in_at == 0.0:
        return COLD_START_MAX_HOURS
    hours_since_opt_in = (time.time() - prefs.opted_in_at) / 3600.0
    return min(COLD_START_MAX_HOURS, max(1, int(hours_since_opt_in) + 1))


# ---------- Fetch ----------

def fetch_user_window(prefs: UserPrefs) -> list[SlackMessage]:
    """Gather all messages relevant to the user in the current window."""
    window_hours = compute_window_hours(prefs)
    oldest = time.time() - window_hours * 3600

    all_messages: list[SlackMessage] = []
    seen_threads: set[tuple[str, str]] = set()

    # Channels the user is in
    channels = slack_client.list_user_channels(
        prefs.user_id, max_channels=MAX_CHANNELS_PER_USER
    )
    channels = [c for c in channels if c.id not in prefs.excluded_channels]

    for channel in channels:
        msgs = slack_client.fetch_channel_messages(channel.id, oldest_epoch=oldest)
        all_messages.extend(msgs)
        # Expand threads with activity in the window
        for m in msgs:
            if m.thread_ts and (channel.id, m.thread_ts) not in seen_threads:
                replies = slack_client.fetch_thread(channel.id, m.thread_ts)
                for r in replies:
                    if float(r.ts) >= oldest:
                        all_messages.append(r)
                seen_threads.add((channel.id, m.thread_ts))

    # DMs (opt-in for the bot to read via im:history)
    for dm in slack_client.list_user_dms(prefs.user_id):
        msgs = slack_client.fetch_channel_messages(dm.id, oldest_epoch=oldest)
        all_messages.extend(msgs)

    return all_messages


def filter_for_user(messages: list[SlackMessage], user_id: str) -> list[SlackMessage]:
    """Drop noise and dedupe by (channel, ts)."""
    seen: set[tuple[str, str]] = set()
    out: list[SlackMessage] = []
    for m in messages:
        key = (m.channel, m.ts)
        if key in seen:
            continue
        seen.add(key)
        if is_noise(m):
            continue
        out.append(m)
    return out


def resolve_permalinks(messages: list[SlackMessage]) -> list[SlackMessage]:
    """Populate `.permalink` on each message. Mutates and returns the same list."""
    for m in messages:
        if m.permalink is None:
            m.permalink = slack_client.get_permalink(m.channel, m.ts) or ""
    return messages


# ---------- Prompt input shaping ----------

def render_message_block(m: SlackMessage) -> str:
    """Wrap one message in a data-only delimiter. Prompt-injection defense lives here."""
    author = m.user or "unknown"
    # Escape to prevent the content from breaking out of the wrapper. Simple
    # approach: neutralize </slack_message> substrings.
    body = m.text.replace("</slack_message>", "</slack_message_escaped>")
    return (
        f'<slack_message id="{m.ts}" author="{author}" channel="{m.channel}" '
        f'ts="{m.ts}" permalink="{m.permalink or ""}">'
        f"{body}"
        f"</slack_message>"
    )


def render_input(messages: list[SlackMessage]) -> str:
    return "\n".join(render_message_block(m) for m in messages)


# ---------- Claude call ----------

def load_prompt() -> str:
    return PROMPT_PATH.read_text()


def call_claude(user_name: str, user_id: str, window_hours: int, wrapped_input: str) -> dict:
    system_template = load_prompt()
    system = (
        system_template
        .replace("{user_name}", user_name)
        .replace("{user_id}", user_id)
        .replace("{window_hours}", str(window_hours))
    )

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system,
        messages=[
            {
                "role": "user",
                "content": (
                    "Messages for the window:\n\n" + wrapped_input +
                    "\n\nEmit the JSON now."
                ),
            }
        ],
    )

    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()

    # Strip markdown fence if Claude returned one
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    return json.loads(text)


# ---------- Block Kit rendering ----------

def render_blocks(user_name: str, themes: list[str], todos: list[dict]) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🌅 Morning, {user_name} — your Slack for today"},
        }
    ]

    if themes:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*What happened*"},
        })
        for t in themes:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"• {t}"},
            })

    blocks.append({"type": "divider"})

    if todos:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Your to-dos*"},
        })
        for td in todos:
            task = td.get("task", "").strip()
            why = td.get("why", "").strip()
            cites = td.get("citations") or []
            links = " · ".join(
                f"<{c}|Jump to message>" for c in cites if c
            )
            line = f"☐ *{task}*"
            if why:
                line += f"\n _{why}_"
            if links:
                line += f"\n {links}"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": line},
            })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No action items for you from the last day. Enjoy._"},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": "`/digest` to refresh · `/research since:YYYY-MM-DD` for bookmarked deep research · DM _digest off_ to pause",
        }],
    })
    return blocks


# ---------- Orchestration ----------

def run_digest_for_user(user_id: str) -> dict:
    """Full pipeline for one user. Returns a structured result for logging."""
    prefs = state.get_user(user_id)
    if not prefs.opted_in:
        return {"user_id": user_id, "skipped": "not opted in"}

    user_info = slack_client.get_user_info(user_id)
    user_name = user_info.get("real_name") or user_info.get("name") or user_id

    window_hours = compute_window_hours(prefs)

    raw = fetch_user_window(prefs)
    filtered = filter_for_user(raw, user_id)
    filtered = resolve_permalinks(filtered)

    input_permalinks = {m.permalink for m in filtered if m.permalink}
    if not filtered:
        # Nothing to say — don't spam the user with an empty digest.
        return {"user_id": user_id, "skipped": "no content in window"}

    wrapped = render_input(filtered)
    try:
        result = call_claude(user_name, user_id, window_hours, wrapped)
    except Exception as exc:
        log.exception("Claude call failed for %s: %s", user_id, exc)
        return {"user_id": user_id, "error": str(exc)}

    themes = result.get("themes") or []
    todos = result.get("todos") or []

    validation = validate.validate_todos(todos, input_permalinks)
    kept = validation.kept_todos

    blocks = render_blocks(user_name, themes, kept)
    fallback = f"Morning digest for {user_name}: {len(kept)} to-dos, {len(themes)} themes."

    try:
        slack_client.post_dm(user_id, text=fallback, blocks=blocks)
    except Exception as exc:
        log.exception("post_dm failed for %s: %s", user_id, exc)
        return {"user_id": user_id, "error": f"post_dm: {exc}"}

    # Mark fired — use user's local date
    local_date = datetime.now().astimezone().strftime("%Y-%m-%d")
    state.mark_digest_fired(user_id, local_date)

    return {
        "user_id": user_id,
        "themes": len(themes),
        "todos_kept": len(kept),
        "todos_dropped": validation.dropped_count,
        "messages_scanned": len(filtered),
        "window_hours": window_hours,
    }
