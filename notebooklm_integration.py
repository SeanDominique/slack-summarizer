"""NotebookLM wrapper — production version.

Restores the Google session cookie from a Modal secret into ~/.notebooklm/ at
runtime, submits the synthesis document as a pasted-text source, generates an
audio overview, and returns a `PodcastResult` containing the notebook URL and
(when downloadable) the MP3 bytes.

Failure mode strategy:
    - If we can create the notebook + add the source, we always have a usable
      URL pointing at the notebook in NotebookLM's web app. Even if our MP3
      download fails, the user can click that URL and listen there directly.
    - The download polling has historically been brittle (notebooklm-py's own
      `wait_for_completion` has a known issue where it hangs on the
      `_is_media_ready` check, and `Artifact.status` is an int, not a string).
      So we always return a URL-bearing result rather than raising — the
      caller decides the DM format.
    - Only raise `NotebookLMError` when notebook creation itself fails (auth
      broken, library import error, etc.) — those are real failures with no
      fallback path.

Defaults: `AudioLength.SHORT` + `AudioFormat.BRIEF` produces ~2-5 min podcasts.
NotebookLM's default is LONG (~15 min) which is overkill for bookmark
synthesis.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from notebooklm import NotebookLMClient
from notebooklm.rpc.types import ArtifactStatus, AudioFormat, AudioLength
from notebooklm.types import ArtifactNotReadyError, ArtifactParseError


@dataclass
class PodcastResult:
    """Outcome of a podcast generation attempt.

    notebook_url is always present (the notebook always exists if we reached
    the point of returning). audio_bytes is present iff the MP3 download
    succeeded — when it's None, the caller should DM the URL and let the user
    play the audio in NotebookLM's web app.
    """
    notebook_id: str
    notebook_url: str
    audio_bytes: bytes | None
    download_error: str | None  # populated if audio_bytes is None

log = logging.getLogger(__name__)

# Total wall-clock budget for the whole flow (submit + generate + download).
# Audio generation itself usually completes in 3-8 min for SHORT length.
NOTEBOOKLM_TIMEOUT_S = 900.0

# Polling cadence for list_audio. NotebookLM's status endpoint is cheap.
POLL_INITIAL_S = 5.0
POLL_MAX_S = 20.0


class NotebookLMError(Exception):
    """Any failure that should trigger the text-only fallback."""


def _restore_auth_from_env() -> None:
    """Write ~/.notebooklm/storage_state.json from NOTEBOOKLM_STORAGE_STATE_B64."""
    raw = os.environ.get("NOTEBOOKLM_STORAGE_STATE_B64")
    if not raw:
        raise NotebookLMError("NOTEBOOKLM_STORAGE_STATE_B64 not set in environment")
    auth_dir = Path.home() / ".notebooklm"
    auth_dir.mkdir(exist_ok=True)
    (auth_dir / "storage_state.json").write_bytes(base64.b64decode(raw))


async def _wait_then_download(client, notebook_id: str, target_task_id: str | None, out_path: str) -> None:
    """Poll until an audio artifact for this notebook is downloadable, then save it.

    Skips `wait_for_completion()` to dodge the `_is_media_ready` polling bug.

    Logic: every N seconds, list audio artifacts; once any has the COMPLETED
    status code (integer 3, NOT a string), try `download_audio()`. If
    `ArtifactNotReadyError` or `ArtifactParseError` fires (URL field not yet
    populated even though status is COMPLETED), wait and retry.

    NB: Artifact.status is an int (ArtifactStatus enum). Comparing strings
    silently fails — that bug ate 15 minutes of polling on the previous run.
    """
    start = asyncio.get_running_loop().time()
    interval = POLL_INITIAL_S
    poll_count = 0

    while True:
        elapsed = asyncio.get_running_loop().time() - start
        if elapsed > NOTEBOOKLM_TIMEOUT_S:
            raise NotebookLMError(
                f"Timeout after {NOTEBOOKLM_TIMEOUT_S:.0f}s waiting for audio "
                f"artifact (notebook {notebook_id}, task {target_task_id})"
            )

        try:
            audios = await client.artifacts.list_audio(notebook_id)
        except Exception as exc:
            log.warning("list_audio failed mid-poll: %s — retrying", exc)
            audios = []

        poll_count += 1
        # Lightweight log every poll so debugging future failures is cheap.
        statuses = [(a.id[:8], int(a.status)) for a in audios]
        log.info(
            "poll #%d (%.0fs elapsed): %d audio artifact(s) %s",
            poll_count, elapsed, len(audios), statuses,
        )

        completed = [a for a in audios if int(a.status) == ArtifactStatus.COMPLETED.value]
        if completed:
            try:
                await client.artifacts.download_audio(notebook_id, out_path)
                log.info(
                    "audio downloaded after %.1fs (artifact %s)",
                    elapsed, completed[0].id[:8],
                )
                return
            except (ArtifactNotReadyError, ArtifactParseError) as exc:
                log.info("status COMPLETED but download not ready yet (%s); retrying", exc)
            except Exception as exc:
                log.warning("download_audio raised %r — retrying", exc)

        await asyncio.sleep(interval)
        interval = min(interval * 1.5, POLL_MAX_S)


def _notebook_url(notebook_id: str) -> str:
    return f"https://notebooklm.google.com/notebook/{notebook_id}"


async def generate_podcast(
    source_title: str,
    source_markdown: str,
    notebook_name: str,
    instructions: str = "Keep it concise — match length to substance.",
    audio_length: AudioLength = AudioLength.SHORT,
    audio_format: AudioFormat = AudioFormat.BRIEF,
) -> PodcastResult:
    """Generate a podcast and return a PodcastResult.

    Always returns a result if notebook creation succeeded — never raises on
    download/timeout failures. Caller checks `audio_bytes` and `download_error`
    to decide DM format.

    Raises NotebookLMError ONLY if we couldn't even create the notebook
    (auth/library failure with no fallback path).

    Defaults: SHORT length (~2-5 min) + BRIEF format. Override for richer
    corpora.
    """
    _restore_auth_from_env()

    # Phase 1: notebook + source. Failures here are unrecoverable.
    try:
        client_ctx = NotebookLMClient.from_storage()
        client = await client_ctx
    except Exception as e:
        raise NotebookLMError(f"NotebookLM auth/init failed: {e!r}") from e

    try:
        async with client:
            try:
                nb = await client.notebooks.create(notebook_name)
                log.info("created notebook %s", nb.id)
                await client.sources.add_text(
                    nb.id, source_title, source_markdown, wait=True
                )
                log.info("added source to notebook %s", nb.id)
            except Exception as e:
                raise NotebookLMError(
                    f"Failed to create notebook + source: {e!r}"
                ) from e

            url = _notebook_url(nb.id)

            # Phase 2: kick off audio generation. If this fails, we still have
            # a notebook URL the user can visit (and manually click "Generate
            # Audio Overview" themselves if needed).
            try:
                status = await client.artifacts.generate_audio(
                    nb.id,
                    instructions=instructions,
                    audio_format=audio_format,
                    audio_length=audio_length,
                )
                log.info(
                    "audio generation kicked off task=%s; polling for completion",
                    status.task_id,
                )
            except Exception as e:
                log.warning("generate_audio kickoff failed: %r", e)
                return PodcastResult(
                    notebook_id=nb.id,
                    notebook_url=url,
                    audio_bytes=None,
                    download_error=f"audio kickoff failed: {e!r}",
                )

            # Phase 3: poll + download. If anything fails, fall back to URL.
            try:
                out_path = "/tmp/podcast.mp3"
                await _wait_then_download(client, nb.id, status.task_id, out_path)
                audio_bytes = Path(out_path).read_bytes()
                return PodcastResult(
                    notebook_id=nb.id,
                    notebook_url=url,
                    audio_bytes=audio_bytes,
                    download_error=None,
                )
            except NotebookLMError as e:
                # Polling timed out, but the audio likely exists in the
                # notebook UI. URL fallback gives the user a working path.
                log.info("download fell back to URL-only: %s", e)
                return PodcastResult(
                    notebook_id=nb.id,
                    notebook_url=url,
                    audio_bytes=None,
                    download_error=str(e),
                )
            except Exception as e:
                log.warning("unexpected error during download: %r", e)
                return PodcastResult(
                    notebook_id=nb.id,
                    notebook_url=url,
                    audio_bytes=None,
                    download_error=f"unexpected: {e!r}",
                )
    except NotebookLMError:
        raise
    except Exception as e:
        raise NotebookLMError(f"NotebookLM session error: {e!r}") from e


def generate_podcast_sync(
    source_title: str,
    source_markdown: str,
    notebook_name: str,
) -> PodcastResult:
    """Sync entrypoint for Modal functions that aren't declared async."""
    return asyncio.run(
        generate_podcast(source_title, source_markdown, notebook_name)
    )
