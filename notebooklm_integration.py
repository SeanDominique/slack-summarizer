"""NotebookLM wrapper — production version.

Restores the Google session cookie from a Modal secret into ~/.notebooklm/ at
runtime, submits the synthesis document as a pasted-text source, generates an
audio overview, and returns the local MP3 bytes.

Two notes from real-world use:

1. We do NOT use `client.artifacts.wait_for_completion()`. That helper runs
   `poll_status()` which calls `_is_media_ready()` — a check that demands the
   internal media URL field be populated before reporting COMPLETED. That field
   sometimes lags behind the actual completion by 5+ minutes (audio shows up in
   the NotebookLM UI but the polling helper still says "processing"). Our
   workaround is to poll `list_audio()` ourselves and just try
   `download_audio()` directly, which only requires status==COMPLETED.

2. We request `AudioLength.SHORT` + `AudioFormat.BRIEF` by default — the
   library's default produces 15+ minute podcasts which is overkill for typical
   bookmark synthesis (a few hundred words of content).

On any failure (timeout, auth, library breakage) the caller is expected to
fall back to DMing the markdown synthesis.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path

from notebooklm import NotebookLMClient
from notebooklm.rpc.types import ArtifactStatus, AudioFormat, AudioLength
from notebooklm.types import ArtifactNotReadyError, ArtifactParseError

log = logging.getLogger(__name__)

# Total wall-clock budget for the whole flow (submit + generate + download).
# Audio generation itself usually completes in 3-8 min for SHORT length.
NOTEBOOKLM_TIMEOUT_S = 900.0

# Polling cadence for list_audio. NotebookLM's status endpoint is cheap.
POLL_INITIAL_S = 5.0
POLL_MAX_S = 20.0


class NotebookLMError(Exception):
    """Any failure that should trigger the text-only fallback.

    If the failure happened AFTER the notebook was created (most common case —
    download timed out, but the audio is still being generated in the
    notebook), `notebook_url` is set so the caller can include it in the
    Slack DM. The user can then click through and listen on NotebookLM
    directly even though we couldn't pull the MP3.
    """

    def __init__(self, message: str, notebook_url: str | None = None):
        super().__init__(message)
        self.notebook_url = notebook_url


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


async def generate_podcast(
    source_title: str,
    source_markdown: str,
    notebook_name: str,
    instructions: str = "Keep it concise — match length to substance.",
    audio_length: AudioLength = AudioLength.SHORT,
    audio_format: AudioFormat = AudioFormat.BRIEF,
) -> tuple[bytes, str]:
    """Returns (MP3 bytes, notebook URL). Raises NotebookLMError on any failure.

    On post-creation failures the raised NotebookLMError carries the notebook
    URL via its `.notebook_url` attribute so the caller can still link the
    user to the NotebookLM web app.

    Defaults: SHORT length (~2-5 min) + BRIEF format. Override for richer
    corpora.
    """
    _restore_auth_from_env()

    nb_url: str | None = None
    try:
        async with await NotebookLMClient.from_storage() as client:
            nb = await client.notebooks.create(notebook_name)
            nb_url = f"https://notebooklm.google.com/notebook/{nb.id}"
            log.info("created notebook %s", nb.id)

            await client.sources.add_text(
                nb.id, source_title, source_markdown, wait=True
            )
            log.info("added source to %s", nb.id)

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

            out_path = "/tmp/podcast.mp3"
            await _wait_then_download(client, nb.id, status.task_id, out_path)
            return Path(out_path).read_bytes(), nb_url
    except NotebookLMError as e:
        # _wait_then_download raised. Re-raise with URL attached if we have
        # one (we will, since notebook creation always runs first).
        if e.notebook_url is None and nb_url is not None:
            raise NotebookLMError(str(e), notebook_url=nb_url) from e
        raise
    except asyncio.TimeoutError as e:
        raise NotebookLMError(f"NotebookLM timeout: {e}", notebook_url=nb_url) from e
    except Exception as e:
        raise NotebookLMError(
            f"NotebookLM generation failed: {e!r}", notebook_url=nb_url
        ) from e


def generate_podcast_sync(
    source_title: str,
    source_markdown: str,
    notebook_name: str,
) -> tuple[bytes, str]:
    """Sync entrypoint for Modal functions that aren't declared async."""
    return asyncio.run(
        generate_podcast(source_title, source_markdown, notebook_name)
    )
