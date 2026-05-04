# Blok Digest Bot

A self-hosted Slack bot that DMs you a personalized morning digest of your last
24 hours of Slack — themes + a to-do list with deep links back to the source
messages. Plus: react any message with :bookmark:, then run `/research
since:2026-03-01` to get an audio podcast synthesizing every bookmark you've
piled up since that date.

Each user runs their own copy in their own Slack workspace. No central server,
no shared accounts, no marketplace listing. You pay your own Anthropic /
Modal / Google bills (typically ~$25–$35/month for a 20-person team).

## What you'll need

Before you start (~15 minutes total to gather these):

| | What | How |
|---|---|---|
| 1 | A Slack workspace where you're an admin | Your personal workspace works |
| 2 | An [Anthropic API key](https://console.anthropic.com/settings/keys) | 2 min to create; needs a credit card on file |
| 3 | A free [Modal account](https://modal.com/signup) | Hosts the bot. Free tier covers personal use |
| 4 | A Google account with [NotebookLM access](https://notebooklm.google.com) | Used by `/research` to generate the audio podcasts |
| 5 | [`uv`](https://docs.astral.sh/uv/) installed locally | One command: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| 6 | Python 3.12+ (`uv` will install it for you if missing) | |

## Install

```bash
# 1. Clone
git clone https://github.com/SeanDominique/slack-summarizer.git
cd slack-summarizer

# 2. Install dependencies
uv sync

# 3. Sign into NotebookLM (opens a browser; sign into your Google account)
uv run notebooklm login
uv run notebooklm auth check --test

# 4. Sign into Modal (opens a browser; create account if needed)
uv run modal setup

# 5. Run the interactive installer
uv run python setup.py
```

The `setup.py` script:
- Creates the Modal secrets it needs (Anthropic key, your NotebookLM cookie)
- Deploys the bot to Modal so you can get the public webhook URL
- Generates a `generated_manifest.yaml` with that URL filled in for Slack
- Walks you through creating the Slack App from the manifest, getting the
  bot token + signing secret
- Adds those to Modal and redeploys

It then prints the final 3 manual steps in the Slack UI (event subscriptions,
reinstall app, test) — you'll do those last bits in your browser.

Total time, end to end: ~15-20 minutes. Most of it is waiting for the Modal
image to build the first time.

## Test it

After setup completes, in any Slack channel or DM:

```
/digest on        # opt yourself in to the morning DM
/digest           # send a digest right now to your DMs
```

To test the research feature:

```
React to a few messages with :bookmark:
/research since:2026-04-01
```

You'll get an ephemeral ack within 1 second, and the actual digest / podcast
DM lands ~30s for digest, 3-10 min for research.

## Re-deploys

After editing the code (e.g. tuning a prompt):

```bash
uv run modal deploy main.py
```

Hot-swap, no Slack reconfiguration needed.

## Architecture

```
Slack workspace
   │ slash commands + events
   ▼
Modal webhook (FastAPI + Slack Bolt)
   │
   ├─► Cron (weekday 09:00 local) → spawns per-user digest function
   │     └─► Slack Web API   ─►  Claude Sonnet (summarize + extract todos)
   │           ─►  Block Kit DM with deep links
   │
   └─► /research command → spawns research function
         └─► reactions index → cluster (Claude) → fan-out summaries (Claude, parallel)
              ─►  synthesis (Claude) → NotebookLM (audio gen) → DM .mp3
```

State lives in Modal Dicts (per-user prefs, reactions index, rate-limit bucket).
No external database. The full design doc is at
`~/.gstack/projects/slack-summarizer/seandominique-main-design-20260423-230925.md`
in the original developer's workspace if you want the rationale for every
decision.

## Things that will trip you up

- **NotebookLM cookie expires roughly weekly.** When `/research` starts failing
  with `NotebookLMError: NotebookLM generation failed`, run `uv run notebooklm
  login` again, then re-run `uv run python setup.py` (it'll skip the steps you
  already did and just refresh the cookie).
- **The bot can't see channels it isn't a member of.** Invite the bot to every
  channel you want digested. `/invite @blok_digest` in each channel.
- **The bot can't see Slack messages bookmarked before it was installed.** The
  reactions index only captures bookmarks from install forward.
- **`notebooklm-py` is unofficial.** It uses undocumented Google APIs that can
  break with no warning. If `/research` is broken for everyone, check the
  [notebooklm-py issue tracker](https://github.com/teng-lin/notebooklm-py/issues).

## Costs

At a 20-person team running daily digests + ~50 research sessions/month:

| | Estimate |
|---|---|
| Anthropic API (Sonnet 4.x) | ~$25-30/month |
| Modal compute | $0 on free tier; ~$5/month if you exceed |
| NotebookLM | $0 (consumer Google account) |
| **Total** | **~$25-35/month** |

Lower if it's just you. Anthropic billing kicks in based on tokens, so a quiet
Slack costs less.

## Privacy

This bot reads your Slack messages and sends them to Anthropic's Claude API to
summarize. Your team should know this before you install. Reaction-bookmarked
messages additionally pass through your Google account's NotebookLM. No data
goes anywhere else (no logging service, no analytics, nothing leaves your
Modal account).

## Layout

```
main.py                   Modal app: webhook, cron, spawned digest/research fns
setup.py                  Interactive installer (run this first)
slack_app_manifest.yaml   Slack App definition (template)
slack_client.py           Slack API wrappers
digest.py                 Daily digest pipeline
research.py               Reaction-bookmarked research pipeline
notebooklm_integration.py NotebookLM wrapper with text-fallback
state.py                  Modal Dict wrappers (users, reactions, rate-limit)
validate.py               Citation validator (drops hallucinated permalinks)
prompts/
  daily_digest.md         Load-bearing Feature 1 prompt — tune this on real data
  cluster_themer.md       Feature 2 step 1
  theme_summarizer.md     Feature 2 step 2
notebooklm_smoke_test.py  Local NotebookLM smoke test
modal_smoke_test.py       Modal-egress NotebookLM smoke test
```

## License

MIT — see [LICENSE](LICENSE).
