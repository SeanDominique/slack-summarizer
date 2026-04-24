"""NotebookLM wrapper — production version.

Restores the Google session cookie from a Modal secret into ~/.notebooklm/ at
runtime, submits the synthesis document as a pasted-text source, generates an
audio overview, and returns the local MP3 bytes.

Hard 5-minute generation timeout. On any failure (timeout, auth, library
breakage) the caller is expected to fall back to DMing the markdown synthesis.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path

from notebooklm import NotebookLMClient

log = logging.getLogger(__name__)

NOTEBOOKLM_TIMEOUT_S = 300.0


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


async def generate_podcast(
    source_title: str,
    source_markdown: str,
    notebook_name: str,
    instructions: str = "make it engaging, walk the listener through the themes",
) -> bytes:
    """Returns the MP3 bytes. Raises NotebookLMError on any failure."""
    _restore_auth_from_env()

    try:
        async with await NotebookLMClient.from_storage() as client:
            nb = await client.notebooks.create(notebook_name)
            log.info("created notebook %s", nb.id)

            await client.sources.add_text(
                nb.id, source_title, source_markdown, wait=True
            )
            log.info("added source to %s", nb.id)

            status = await client.artifacts.generate_audio(
                nb.id, instructions=instructions
            )
            await client.artifacts.wait_for_completion(
                nb.id, status.task_id, timeout=NOTEBOOKLM_TIMEOUT_S
            )
            log.info("audio generation complete for %s", nb.id)

            out_path = "/tmp/podcast.mp3"
            await client.artifacts.download_audio(nb.id, out_path)
            return Path(out_path).read_bytes()
    except asyncio.TimeoutError as e:
        raise NotebookLMError(f"NotebookLM timeout after {NOTEBOOKLM_TIMEOUT_S}s") from e
    except TimeoutError as e:
        raise NotebookLMError(f"NotebookLM task timeout: {e}") from e
    except Exception as e:
        raise NotebookLMError(f"NotebookLM generation failed: {e}") from e


def generate_podcast_sync(
    source_title: str,
    source_markdown: str,
    notebook_name: str,
) -> bytes:
    """Sync entrypoint for Modal functions that aren't declared async."""
    return asyncio.run(
        generate_podcast(source_title, source_markdown, notebook_name)
    )
