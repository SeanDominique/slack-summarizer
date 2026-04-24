"""Slack Web API wrappers.

Everything that talks to Slack lives here. Callers get typed dataclasses back
instead of raw SlackResponse objects so the rest of the codebase doesn't import
from slack_sdk.

Bot-token only — no user-token (xoxp) paths. `search.messages` is deliberately
unused (requires user token); @mentions come from message-event indexing or from
local scanning of channel history.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Iterator

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from state import ratelimit_acquire

log = logging.getLogger(__name__)

SLACK_READ_BUCKET = "slack-read"
SLACK_READ_LIMIT_PER_MIN = 45  # conservative vs Slack tier-3 (50/min)


@dataclass
class SlackMessage:
    channel: str
    ts: str
    user: str | None  # None for some bot messages
    text: str
    thread_ts: str | None
    subtype: str | None
    permalink: str | None = None  # filled in lazily by get_permalink


@dataclass
class ChannelInfo:
    id: str
    name: str
    is_private: bool
    is_im: bool
    is_mpim: bool
    is_archived: bool


def client() -> WebClient:
    token = os.environ["SLACK_BOT_TOKEN"]
    return WebClient(token=token)


# ---------- Rate-limit-aware call helper ----------

def _call_with_backoff(fn, *args, **kwargs):
    """Run a Slack API call with client-side rate limiting + 429 backoff.

    Up to 5 retries. Caller is responsible for its own budget cap.
    """
    for attempt in range(5):
        while not ratelimit_acquire(SLACK_READ_BUCKET, SLACK_READ_LIMIT_PER_MIN):
            time.sleep(1.0)
        try:
            return fn(*args, **kwargs)
        except SlackApiError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", "5"))
                log.warning("Slack 429; sleeping %ds", retry_after)
                time.sleep(retry_after)
                continue
            raise
    raise RuntimeError("Exhausted Slack API retries")


# ---------- Channel / user discovery ----------

def list_user_channels(user_id: str, max_channels: int = 50) -> list[ChannelInfo]:
    """Channels (public + private + mpim) the user is a member of, excluding archived.

    Truncates to max_channels most recently active if the user belongs to more.
    """
    wc = client()
    channels: list[ChannelInfo] = []
    cursor = None
    while True:
        resp = _call_with_backoff(
            wc.users_conversations,
            user=user_id,
            types="public_channel,private_channel,mpim",
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        for c in resp["channels"]:
            channels.append(
                ChannelInfo(
                    id=c["id"],
                    name=c.get("name", c.get("id", "")),
                    is_private=c.get("is_private", False),
                    is_im=False,
                    is_mpim=c.get("is_mpim", False),
                    is_archived=c.get("is_archived", False),
                )
            )
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels[:max_channels]


def list_user_dms(user_id: str) -> list[ChannelInfo]:
    """All IM conversations the user participates in (bot must share them)."""
    wc = client()
    dms: list[ChannelInfo] = []
    cursor = None
    while True:
        resp = _call_with_backoff(
            wc.users_conversations,
            user=user_id,
            types="im",
            limit=200,
            cursor=cursor,
        )
        for c in resp["channels"]:
            dms.append(
                ChannelInfo(
                    id=c["id"],
                    name=c.get("user", c["id"]),
                    is_private=True,
                    is_im=True,
                    is_mpim=False,
                    is_archived=False,
                )
            )
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return dms


# ---------- Message fetch ----------

def fetch_channel_messages(
    channel_id: str,
    oldest_epoch: float,
) -> list[SlackMessage]:
    """Top-level messages in a channel since `oldest_epoch`."""
    wc = client()
    results: list[SlackMessage] = []
    cursor = None
    while True:
        resp = _call_with_backoff(
            wc.conversations_history,
            channel=channel_id,
            oldest=str(oldest_epoch),
            limit=200,
            cursor=cursor,
        )
        for m in resp["messages"]:
            if m.get("subtype") in {
                "channel_join", "channel_leave", "channel_topic",
                "channel_purpose", "channel_name",
            }:
                continue
            results.append(
                SlackMessage(
                    channel=channel_id,
                    ts=m["ts"],
                    user=m.get("user") or m.get("bot_id"),
                    text=m.get("text", ""),
                    thread_ts=m.get("thread_ts"),
                    subtype=m.get("subtype"),
                )
            )
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor or not resp.get("has_more"):
            break
    return results


def fetch_thread(channel_id: str, thread_ts: str) -> list[SlackMessage]:
    """All replies in a thread (including the parent)."""
    wc = client()
    resp = _call_with_backoff(
        wc.conversations_replies,
        channel=channel_id,
        ts=thread_ts,
        limit=200,
    )
    results: list[SlackMessage] = []
    for m in resp["messages"]:
        results.append(
            SlackMessage(
                channel=channel_id,
                ts=m["ts"],
                user=m.get("user") or m.get("bot_id"),
                text=m.get("text", ""),
                thread_ts=m.get("thread_ts"),
                subtype=m.get("subtype"),
            )
        )
    return results


def get_permalink(channel_id: str, message_ts: str) -> str | None:
    wc = client()
    try:
        resp = _call_with_backoff(
            wc.chat_getPermalink,
            channel=channel_id,
            message_ts=message_ts,
        )
        return resp.get("permalink")
    except SlackApiError as e:
        log.warning("getPermalink failed for %s/%s: %s", channel_id, message_ts, e)
        return None


# ---------- Send ----------

def post_dm(user_id: str, text: str, blocks: list | None = None) -> str:
    """Open an IM with the user and post. Returns the IM channel id."""
    wc = client()
    opened = _call_with_backoff(wc.conversations_open, users=user_id)
    channel_id = opened["channel"]["id"]
    _call_with_backoff(
        wc.chat_postMessage,
        channel=channel_id,
        text=text,
        blocks=blocks or [],
    )
    return channel_id


def post_ephemeral_ack(channel: str, user: str, text: str) -> None:
    """Quick ack to a slash command — visible only to the invoker."""
    wc = client()
    _call_with_backoff(
        wc.chat_postEphemeral, channel=channel, user=user, text=text
    )


def get_user_info(user_id: str) -> dict:
    wc = client()
    resp = _call_with_backoff(wc.users_info, user=user_id)
    return resp["user"]


# ---------- Helpers ----------

def iter_user_visible_channels(user_id: str, max_channels: int = 50) -> Iterator[ChannelInfo]:
    """Channels + DMs for a user, capped."""
    yielded = 0
    for c in list_user_channels(user_id, max_channels=max_channels):
        yield c
        yielded += 1
        if yielded >= max_channels:
            return
    for dm in list_user_dms(user_id):
        yield dm
        yielded += 1
        if yielded >= max_channels:
            return
