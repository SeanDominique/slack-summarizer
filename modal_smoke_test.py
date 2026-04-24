import base64
import os
from pathlib import Path

import modal
from notebooklm import NotebookLMClient

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("notebooklm-py[browser]")
    .run_commands("playwright install-deps chromium", "playwright install chromium")
)

app = modal.App("nlm-smoke-test", image=image)


@app.function(
    secrets=[modal.Secret.from_name("notebooklm-auth")],
    timeout=1200,  # 20 min — audio gen can queue longer on new-IP logins
)
async def smoke_test():
    # Restore the storage state from the secret into the path notebooklm-py expects
    auth_dir = Path.home() / ".notebooklm"
    auth_dir.mkdir(exist_ok=True)
    (auth_dir / "storage_state.json").write_bytes(
        base64.b64decode(os.environ["NOTEBOOKLM_STORAGE_STATE_B64"])
    )

    SOURCE = "Blok engineering shipped an auth refactor this week. Maya led it, Dave reviewed. Zero incidents."

    async with await NotebookLMClient.from_storage() as client:
        nb = await client.notebooks.create("Modal Smoke Test")
        await client.sources.add_text(nb.id, "Weekly", SOURCE, wait=True)
        status = await client.artifacts.generate_audio(
            nb.id, instructions="brief, ~3 min"
        )
        await client.artifacts.wait_for_completion(
            nb.id, status.task_id, timeout=900.0
        )
        # Download to /tmp and return bytes
        out_path = "/tmp/podcast.mp3"
        await client.artifacts.download_audio(nb.id, out_path)
        return Path(out_path).read_bytes()


@app.local_entrypoint()
def main():
    audio = smoke_test.remote()
    Path("modal_podcast.mp3").write_bytes(audio)
    print(f"✅ got {len(audio)} bytes")
