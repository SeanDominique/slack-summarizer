"""Citation validator.

The daily digest prompt asks Claude to cite Slack permalinks. Claude sometimes
paraphrases URLs — drops a trailing slash, swaps casing on the host, confuses a
thread parent with its reply. This module normalizes both sides and drops any
to-do whose cited permalinks don't appear in the original input set.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


@dataclass
class ValidationResult:
    kept_todos: list[dict]
    dropped_count: int
    dropped_reasons: list[str]


def normalize_permalink(url: str) -> str:
    """Canonicalize a Slack permalink for comparison.

    - lowercase host
    - strip trailing slash on path
    - keep only the thread_ts query parameter (drop cid, etc.)
    - drop fragment
    """
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    path = parsed.path.rstrip("/")
    qs = parse_qs(parsed.query)
    keep = {}
    if "thread_ts" in qs:
        keep["thread_ts"] = qs["thread_ts"][0]
    query = urlencode(keep)
    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def validate_todos(
    todos: list[dict],
    input_permalinks: set[str],
) -> ValidationResult:
    """Keep only todos whose citations all match a normalized input permalink.

    Each todo is a dict with at least `citations: list[str]`.
    """
    normalized_inputs = {normalize_permalink(p) for p in input_permalinks if p}
    kept: list[dict] = []
    dropped_reasons: list[str] = []
    dropped = 0

    for todo in todos:
        cites = todo.get("citations") or []
        if not cites:
            dropped += 1
            dropped_reasons.append(f"no citations: {todo.get('task','')[:60]!r}")
            continue
        normalized_cites = [normalize_permalink(c) for c in cites]
        missing = [c for c, norm in zip(cites, normalized_cites) if norm not in normalized_inputs]
        if missing:
            dropped += 1
            dropped_reasons.append(
                f"unknown citation in {todo.get('task','')[:60]!r}: {missing[:2]}"
            )
            continue
        kept.append(todo)

    return ValidationResult(
        kept_todos=kept,
        dropped_count=dropped,
        dropped_reasons=dropped_reasons,
    )
