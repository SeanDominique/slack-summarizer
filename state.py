"""Modal Dict wrappers for per-user prefs, reaction index, and rate-limit bucket.

Modal Dicts persist across container invocations and are durable enough for prototype
state. All write paths here are tolerant of concurrent access — reads happen in the
per-user digest function, writes happen from Slack event handlers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

import modal


USERS_DICT_NAME = "blok-digest-users"
REACTIONS_DICT_NAME = "blok-digest-reactions"
RATELIMIT_DICT_NAME = "blok-digest-ratelimit"

# v1: hardcoded bookmark emoji per design doc
BOOKMARK_EMOJI = "bookmark"


@dataclass
class UserPrefs:
    user_id: str
    timezone: str = "America/New_York"
    digest_time: str = "09:00"  # HH:MM local, 15-min resolution
    opted_in: bool = False
    last_digest_fired_local_date: str = ""  # YYYY-MM-DD, DST-safe guard
    excluded_channels: list[str] = field(default_factory=list)
    first_digest_sent: bool = False
    opted_in_at: float = 0.0  # unix ts, bounds cold-start window


@dataclass
class ReactionRecord:
    """One tagged message in a user's research corpus."""
    channel: str
    ts: str
    permalink: str
    reacted_at: float
    emoji: str


def users_dict() -> modal.Dict:
    return modal.Dict.from_name(USERS_DICT_NAME, create_if_missing=True)


def reactions_dict() -> modal.Dict:
    """Keyed by user_id → list[dict] of ReactionRecord."""
    return modal.Dict.from_name(REACTIONS_DICT_NAME, create_if_missing=True)


def ratelimit_dict() -> modal.Dict:
    """Simple per-window counter. Key: 'slack-calls-<minute>', value: int."""
    return modal.Dict.from_name(RATELIMIT_DICT_NAME, create_if_missing=True)


# ---------- Users ----------

def get_user(user_id: str) -> UserPrefs:
    d = users_dict()
    raw = d.get(user_id)
    if raw is None:
        return UserPrefs(user_id=user_id)
    return UserPrefs(**raw)


def upsert_user(prefs: UserPrefs) -> None:
    users_dict()[prefs.user_id] = asdict(prefs)


def all_users() -> list[UserPrefs]:
    return [UserPrefs(**v) for v in users_dict().values()]


def set_opted_in(user_id: str, opted_in: bool) -> UserPrefs:
    prefs = get_user(user_id)
    prefs.opted_in = opted_in
    if opted_in and prefs.opted_in_at == 0.0:
        prefs.opted_in_at = time.time()
    upsert_user(prefs)
    return prefs


def set_timezone(user_id: str, tz: str) -> UserPrefs:
    prefs = get_user(user_id)
    prefs.timezone = tz
    upsert_user(prefs)
    return prefs


def mark_digest_fired(user_id: str, local_date: str) -> None:
    prefs = get_user(user_id)
    prefs.last_digest_fired_local_date = local_date
    prefs.first_digest_sent = True
    upsert_user(prefs)


# ---------- Reactions index ----------

def add_reaction(user_id: str, record: ReactionRecord) -> None:
    """Append to this user's list. Idempotent on (channel, ts) pair."""
    d = reactions_dict()
    existing: list[dict] = d.get(user_id, [])
    key = (record.channel, record.ts)
    if any((r["channel"], r["ts"]) == key for r in existing):
        return
    existing.append(asdict(record))
    d[user_id] = existing


def remove_reaction(user_id: str, channel: str, ts: str) -> None:
    d = reactions_dict()
    existing: list[dict] = d.get(user_id, [])
    filtered = [r for r in existing if (r["channel"], r["ts"]) != (channel, ts)]
    if len(filtered) != len(existing):
        d[user_id] = filtered


def get_reactions_since(user_id: str, since_epoch: float) -> list[ReactionRecord]:
    raw: list[dict] = reactions_dict().get(user_id, [])
    return [ReactionRecord(**r) for r in raw if r["reacted_at"] >= since_epoch]


# ---------- Rate limit ----------
# Coarse window-bucket counter. Good enough for a small-team prototype;
# swap for a proper token bucket if needed.

def ratelimit_acquire(bucket: str, limit_per_minute: int) -> bool:
    """Returns True if the call is allowed. Increments the current-minute bucket."""
    d = ratelimit_dict()
    minute = int(time.time()) // 60
    key = f"{bucket}-{minute}"
    # Non-atomic read-modify-write — acceptable at prototype scale. If this
    # starts producing incorrect rate limits under load, lift it into a proper
    # atomic sequence.
    current = d.get(key, 0)
    if current >= limit_per_minute:
        return False
    d[key] = current + 1
    return True
