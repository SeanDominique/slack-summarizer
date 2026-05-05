"""Blok Digest Bot — Modal app entrypoint.

Surfaces:
    - HTTPS webhook (FastAPI + Slack Bolt) for slash commands and events
    - Scheduled dispatcher (cron, weekdays every 15 min) that spawns per-user digests
    - Research function (spawned on demand from /research)

Deploy:
    uv run modal deploy main.py

Secrets expected:
    - slack-creds   (SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET)
    - anthropic     (ANTHROPIC_API_KEY)
    - notebooklm-auth (NOTEBOOKLM_STORAGE_STATE_B64)  — only needed for /research
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import modal

# These need to be at module top, not inside slack_webhook(), because
# `from __future__ import annotations` makes every type hint a string and
# FastAPI's get_type_hints() resolves them against module globals — not
# local scope. Hide them inside the asgi function and Request-injection
# silently breaks (FastAPI falls back to treating `req` as a query param).
from fastapi import FastAPI, Request
from slack_bolt import App as BoltApp
from slack_bolt.adapter.fastapi import SlackRequestHandler

# ------- Image -------

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "slack-bolt>=1.18",
        "slack-sdk>=3.27",
        "anthropic>=0.39",
        "fastapi>=0.110",
        "pydantic>=2.7",
        "python-dateutil>=2.9",
        "notebooklm-py[browser]>=0.3.4",
    )
    .run_commands(
        "playwright install-deps chromium",
        "playwright install chromium",
    )
    .add_local_python_source(
        "slack_client", "digest", "research", "state", "validate",
        "notebooklm_integration",
    )
    .add_local_dir(
        str(Path(__file__).parent / "prompts"),
        remote_path="/root/prompts",
    )
)

app = modal.App("blok-digest-bot", image=image)

# Commonly-used secret names
SLACK_SECRET = modal.Secret.from_name("slack-creds")
ANTHROPIC_SECRET = modal.Secret.from_name("anthropic")
NOTEBOOKLM_SECRET = modal.Secret.from_name("notebooklm-auth")

log = logging.getLogger(__name__)


# ---------- Per-user digest function (spawned) ----------

@app.function(
    secrets=[SLACK_SECRET, ANTHROPIC_SECRET],
    timeout=600,
)
def run_user_digest(user_id: str) -> dict:
    import digest
    return digest.run_digest_for_user(user_id)


# ---------- Research function (spawned from /research) ----------

@app.function(
    secrets=[SLACK_SECRET, ANTHROPIC_SECRET, NOTEBOOKLM_SECRET],
    timeout=1500,  # synthesis + NotebookLM poll (up to ~15m total)
)
def run_user_research(user_id: str, since_epoch: float, since_date_str: str) -> dict:
    import base64
    import logging

    import notebooklm_integration
    import research
    import slack_client

    log = logging.getLogger(__name__)
    result = research.run_research(user_id, since_epoch, since_date_str)

    if result.get("status") != "ok":
        _dm_research_failure(user_id, result.get("status", "unknown"))
        return result

    user_name = result["user_name"]
    doc_md = result["document_markdown"]

    # Try NotebookLM; fall back to markdown if it breaks. Either way, include
    # the NotebookLM notebook URL in the DM if we have one — the audio is
    # often generated and visible in the user's NotebookLM account even when
    # our MP3 download into Slack fails.
    try:
        podcast_bytes, notebook_url = notebooklm_integration.generate_podcast_sync(
            source_title=f"Blok Digest — bookmarks since {since_date_str}",
            source_markdown=doc_md,
            notebook_name=f"{user_name} — bookmarks since {since_date_str}",
        )
        _dm_research_success_podcast(
            user_id, user_name, since_date_str,
            podcast_bytes, notebook_url, doc_md, result,
        )
        result["delivery"] = "podcast"
    except notebooklm_integration.NotebookLMError as exc:
        log.warning("NotebookLM failed for %s: %s", user_id, exc)
        notebook_url = getattr(exc, "notebook_url", None)
        _dm_research_success_text_fallback(
            user_id, user_name, since_date_str, doc_md, result,
            notebook_url=notebook_url,
        )
        result["delivery"] = "text_fallback"
        result["notebooklm_error"] = str(exc)

    return result


def _dm_research_failure(user_id: str, status: str) -> None:
    import slack_client
    msg = {
        "empty_corpus": (
            "I couldn't find any messages you'd bookmarked (:bookmark:) in that "
            "date range. React to some messages with :bookmark: and try again."
        ),
        "hydration_failed": (
            "I found your bookmarks but couldn't fetch the message contents. "
            "Some channels may be inaccessible to the bot. Try adding me to those channels."
        ),
        "no_themes": (
            "Your bookmarks didn't cluster into a coherent set of themes. "
            "Try a narrower date range or bookmark a few more messages."
        ),
    }.get(status, f"Research failed: {status}")
    slack_client.post_dm(user_id, msg)


def _dm_research_success_podcast(
    user_id: str, user_name: str, since_date: str,
    podcast_bytes: bytes, notebook_url: str, doc_md: str, result: dict,
) -> None:
    import slack_client
    import os
    from slack_sdk import WebClient

    # Upload podcast as a Slack file attached to the DM
    wc = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    dm = wc.conversations_open(users=user_id)
    channel = dm["channel"]["id"]
    truncated_note = (
        "\n_Note: your corpus was capped at 200 messages — run again with a narrower `since:` for the rest._"
        if result.get("was_truncated") else ""
    )
    wc.files_upload_v2(
        channel=channel,
        content=podcast_bytes,
        filename=f"bookmarks-since-{since_date}.mp3",
        title=f"🎧 {user_name} bookmarks since {since_date}",
        initial_comment=(
            f"Your research podcast is ready — {result['theme_count']} themes across "
            f"{result['corpus_size']} bookmarked messages.{truncated_note}\n"
            f"Listen here in Slack, or open in NotebookLM: <{notebook_url}>"
        ),
    )


def _dm_research_success_text_fallback(
    user_id: str, user_name: str, since_date: str,
    doc_md: str, result: dict,
    *, notebook_url: str | None = None,
) -> None:
    """Fallback DM with markdown attached. If `notebook_url` is set, surface
    it prominently — the audio is likely generated in NotebookLM even though
    we couldn't pull the MP3 into Slack.
    """
    import slack_client
    import os
    from slack_sdk import WebClient

    wc = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    dm = wc.conversations_open(users=user_id)
    channel = dm["channel"]["id"]

    nlm_line = (
        f"\n*The audio podcast itself is ready in NotebookLM:* <{notebook_url}>\n"
        if notebook_url else ""
    )

    wc.files_upload_v2(
        channel=channel,
        content=doc_md.encode("utf-8"),
        filename=f"bookmarks-since-{since_date}.md",
        title=f"📄 {user_name} bookmarks since {since_date}",
        initial_comment=(
            "Research synthesis is ready. The MP3 download into Slack failed "
            "so you're getting the markdown instead."
            + nlm_line
            + f" {result['theme_count']} themes across "
            f"{result['corpus_size']} messages."
        ),
    )


# ---------- Scheduled digest dispatcher ----------

@app.function(
    secrets=[SLACK_SECRET, ANTHROPIC_SECRET],
    schedule=modal.Cron("*/15 * * * 1-5"),  # every 15m on weekdays
    timeout=120,
)
def scheduled_digest_dispatcher() -> dict:
    """Iterate all users; spawn a digest for each one whose local digest time matches.

    Idempotent via `last_digest_fired_local_date` — running twice in the same window
    won't double-fire.
    """
    import state
    now_utc = datetime.utcnow()
    spawned: list[str] = []
    skipped: list[str] = []

    for prefs in state.all_users():
        if not prefs.opted_in:
            skipped.append(f"{prefs.user_id}:opted_out")
            continue
        try:
            tz = ZoneInfo(prefs.timezone)
        except Exception:
            skipped.append(f"{prefs.user_id}:bad_tz:{prefs.timezone}")
            continue
        local_now = now_utc.astimezone(tz)
        local_date = local_now.strftime("%Y-%m-%d")

        # Match within the same 15-min window as the cron
        target_hm = prefs.digest_time
        local_hm = local_now.strftime("%H:%M")
        if not _same_15min_window(target_hm, local_hm):
            skipped.append(f"{prefs.user_id}:not_my_time:{local_hm}")
            continue

        if prefs.last_digest_fired_local_date == local_date:
            skipped.append(f"{prefs.user_id}:already_fired_today")
            continue

        run_user_digest.spawn(prefs.user_id)
        spawned.append(prefs.user_id)

    return {"spawned": spawned, "skipped": skipped}


def _same_15min_window(target_hm: str, current_hm: str) -> bool:
    """target '09:00' matches current in ['09:00','09:01',...,'09:14']."""
    try:
        th, tm = map(int, target_hm.split(":"))
        ch, cm = map(int, current_hm.split(":"))
    except ValueError:
        return False
    return th == ch and (tm // 15) == (cm // 15)


# ---------- Webhook (Slack events + slash commands) ----------

@app.function(
    secrets=[SLACK_SECRET, ANTHROPIC_SECRET],
    min_containers=1,  # keep warm so Slack's 3s ack SLA is safe
    timeout=30,
)
@modal.asgi_app()
def slack_webhook():
    bolt = BoltApp(
        token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
        process_before_response=True,
    )
    _register_handlers(bolt)

    handler = SlackRequestHandler(bolt)

    fastapi_app = FastAPI()

    @fastapi_app.post("/slack/events")
    async def slack_events(req: Request):
        return await handler.handle(req)

    @fastapi_app.get("/health")
    def health():
        return {"ok": True}

    return fastapi_app


SINCE_DATE_RE = re.compile(r"since:(\d{4}-\d{2}-\d{2})")


def _register_handlers(bolt) -> None:
    import state

    @bolt.command("/digest")
    def handle_digest(ack, command, respond):
        text = (command.get("text") or "").strip().lower()
        user_id = command["user_id"]

        if text in ("on", "start", "enable"):
            state.set_opted_in(user_id, True)
            ack(":sunrise: You're opted in. I'll DM you a digest each weekday at 9am local. Change timezone with `/digest timezone America/New_York`.")
            return

        if text in ("off", "stop", "disable"):
            state.set_opted_in(user_id, False)
            ack("Opted out. Run `/digest on` anytime to resume.")
            return

        if text.startswith("timezone "):
            tz = text.split(" ", 1)[1].strip()
            state.set_timezone(user_id, tz)
            ack(f"Timezone set to `{tz}`.")
            return

        # Default: trigger an on-demand digest now
        ack(":sunrise: On it — digest coming to your DMs in ~60s.")
        # Make sure they're opted in so the function proceeds
        prefs = state.get_user(user_id)
        if not prefs.opted_in:
            state.set_opted_in(user_id, True)
        run_user_digest.spawn(user_id)

    @bolt.command("/research")
    def handle_research(ack, command, respond):
        text = (command.get("text") or "").strip()
        user_id = command["user_id"]

        m = SINCE_DATE_RE.search(text)
        if not m:
            ack(
                "Usage: `/research since:YYYY-MM-DD` — I'll build a podcast from "
                "every message you've reacted to with :bookmark: since that date."
            )
            return

        since_date = m.group(1)
        try:
            dt = datetime.strptime(since_date, "%Y-%m-%d")
        except ValueError:
            ack(f"Couldn't parse date `{since_date}`. Use YYYY-MM-DD.")
            return
        since_epoch = dt.timestamp()

        ack(
            ":headphones: On it — fetching your :bookmark: messages since "
            f"{since_date}, clustering, synthesizing, and generating a podcast. "
            "I'll DM you when it's ready (~3-10 min)."
        )
        run_user_research.spawn(user_id, since_epoch, since_date)

    @bolt.event("reaction_added")
    def handle_reaction(event, client):
        reaction = event.get("reaction")
        if reaction != state.BOOKMARK_EMOJI:
            return
        user_id = event.get("user")
        item = event.get("item", {})
        if item.get("type") != "message":
            return
        channel = item.get("channel")
        ts = item.get("ts")
        if not (user_id and channel and ts):
            return

        # Get the permalink
        try:
            permalink_resp = client.chat_getPermalink(channel=channel, message_ts=ts)
            permalink = permalink_resp.get("permalink", "")
        except Exception:
            permalink = ""

        state.add_reaction(
            user_id,
            state.ReactionRecord(
                channel=channel,
                ts=ts,
                permalink=permalink,
                reacted_at=float(event.get("event_ts", "0")),
                emoji=reaction,
            ),
        )

    @bolt.event("reaction_removed")
    def handle_reaction_removed(event):
        if event.get("reaction") != state.BOOKMARK_EMOJI:
            return
        user_id = event.get("user")
        item = event.get("item", {})
        if item.get("type") != "message":
            return
        channel = item.get("channel")
        ts = item.get("ts")
        if user_id and channel and ts:
            state.remove_reaction(user_id, channel, ts)

    @bolt.event("message")
    def handle_message(event):
        # v1: no @mention indexing. digest.py scans channel history for mentions
        # at digest time. Leaving the handler here so Slack's event verification
        # passes — subscribing to message.* events is required for the intended
        # future @mention accumulator.
        pass

    @bolt.event("app_mention")
    def handle_app_mention(event, say):
        say(
            f"Hi <@{event['user']}>! Try `/digest on` to start daily DM digests, "
            "or react to any message with :bookmark: and then run "
            "`/research since:YYYY-MM-DD`."
        )


# ---------- Local entrypoint for one-off testing ----------

@app.local_entrypoint()
def run_once(user_id: str):
    """Run a single user's digest once. Useful for smoke-testing end-to-end.

    Usage: uv run modal run main.py::run_once --user-id U0123
    """
    result = run_user_digest.remote(user_id)
    print(result)
