import asyncio

from notebooklm import NotebookLMClient

SOURCE_DOC = """
# Weekly Team Update — Engineering

This week the engineering team shipped three things:

1. The auth refactor that Maya and Dave had been working on
    for two sprints. It went live Tuesday with zero incidents.

2. A rate-limiter for the public API. Caught a thundering-herd
    edge case on day one and shipped a patch same-day.

3. A new metrics dashboard driven by a single ClickHouse query.
    Replaced three Grafana dashboards that were all showing
    slightly different numbers.

Next week: Maya leads the search-indexing project. Dave moves
to billing. Sean is reviewing the Q2 roadmap with the founders
on Thursday.
"""


async def main():
    async with await NotebookLMClient.from_storage() as client:
        print("Creating notebook...")
        nb = await client.notebooks.create("Smoke Test — Slack Digest")
        print(f"  notebook_id = {nb.id}")

        print("Adding pasted-text source...")
        await client.sources.add_text(
            nb.id, "Weekly Update", SOURCE_DOC, wait=True
        )

        print("Generating audio overview (this can take 5–15 min)...")
        status = await client.artifacts.generate_audio(
            nb.id, instructions="make it engaging"
        )
        await client.artifacts.wait_for_completion(
            nb.id, status.task_id, timeout=900.0
        )

        print("Downloading podcast...")
        await client.artifacts.download_audio(nb.id, "smoke_test_podcast.mp3")
        print("✅ Done. Play smoke_test_podcast.mp3")


if __name__ == "__main__":
    asyncio.run(main())
