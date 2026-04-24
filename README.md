# Blok Digest Bot

A Slack bot that (a) DMs each teammate a personalized morning Slack digest with
action items + message hyperlinks, and (b) on demand, synthesizes the messages
they've bookmarked (via `:bookmark:` reaction) into an audio podcast via
NotebookLM.

Design doc:
`~/.gstack/projects/slack-summarizer/seandominique-main-design-20260423-230925.md`

---

## Status

| Step (per design doc Build Order) | State |
|---|---|
| 1. NotebookLM smoke test (local + Modal) | ✅ done |
| 2. Slack App scaffolding | ⚠️ code ready; Slack App creation + deploy pending |
| 3. Feature 1 fetch + filter + delimiter wrapping | ✅ code scaffold |
| 4. Daily digest prompt tuning | ⚠️ v1 draft in `prompts/daily_digest.md`; needs 12–15h tuning on real data |
| 5. Schedule + state + idempotency | ✅ code scaffold |
| 6. Reaction event accumulator | ✅ code scaffold |
| 7. Feature 2 fetch + cluster + synthesize | ✅ code scaffold |
| 8. Feature 2 NotebookLM integration | ✅ code scaffold (smoke-tested) |
| 9. Reliability pass + dogfood | pending |
| 10. Ship | pending |

---

## Architecture

```
main.py                   Modal app: webhook (Slack Bolt), cron, spawned functions
slack_client.py           Slack Web API wrappers (fetch, send, getPermalink)
digest.py                 Daily digest: fetch → filter → wrap → Claude → validate → DM
research.py               Reaction-bookmarked research: cluster → parallel → synthesize
notebooklm_integration.py Production NotebookLM wrapper (cookie restore + podcast gen)
state.py                  Modal Dict wrappers: users, reactions_index, rate-limit
validate.py               Permalink normalization + citation validator
prompts/
  daily_digest.md         Load-bearing prompt for Feature 1 (needs tuning)
  cluster_themer.md       Feature 2 step 1 — cluster bookmarked messages
  theme_summarizer.md     Feature 2 step 2 — per-theme deep summary
slack_app_manifest.yaml   Paste into Slack API console
modal_smoke_test.py       Phase-2 NotebookLM smoke test (Modal egress)
notebooklm_smoke_test.py  Phase-1 NotebookLM smoke test (local)
```

---

## Deploy (first time)

### 1. Create the Slack App

Go to https://api.slack.com/apps → **Create New App** → **From a manifest** → pick
the Blok workspace. Paste `slack_app_manifest.yaml`. If Slack complains about the
`REPLACE_WITH_MODAL_URL` placeholders, swap them for any valid URL (e.g.
`https://example.com/slack/events`) just to get the app created; you'll fix them in
step 4.

### 2. Install to Workspace + grab credentials

In the Slack app config:

- **OAuth & Permissions** → Install to Workspace → copy the **Bot User OAuth Token**
  (starts with `xoxb-`).
- **Basic Information** → App Credentials → copy the **Signing Secret**.

### 3. Create Modal Secrets

```bash
uv run modal secret create slack-creds \
  SLACK_BOT_TOKEN=xoxb-... \
  SLACK_SIGNING_SECRET=...

uv run modal secret create anthropic \
  ANTHROPIC_API_KEY=sk-ant-...

# notebooklm-auth should already exist from the Phase 2 smoke test.
# If it doesn't, re-create it:
base64 -i ~/.notebooklm/storage_state.json | tr -d '\n' > /tmp/nlm_auth.b64
uv run modal secret create notebooklm-auth \
  NOTEBOOKLM_STORAGE_STATE_B64=$(cat /tmp/nlm_auth.b64)
rm /tmp/nlm_auth.b64
```

### 4. Deploy the app

```bash
uv run modal deploy main.py
```

Modal prints the webhook URL. Looks like:
`https://seandominique--blok-digest-bot-slack-webhook.modal.run`

### 5. Wire the URL back into Slack

In the Slack app config:

- **Slash Commands** → edit each (`/digest`, `/research`) → set Request URL to
  `https://<modal-url>/slack/events`.
- **Event Subscriptions** → set Request URL to the same. Slack sends a challenge
  request; Bolt handles it. The URL should verify green.
- **Install App** (top of sidebar) → Reinstall to Workspace to pick up any scope
  changes.

### 6. Smoke-test in Slack

In any channel or DM with the bot:

```
/digest on
/digest
```

You should see:
- Ephemeral ack: "🌅 On it — digest coming to your DMs in ~60s."
- ~60s later: a DM with a morning digest (even if sparse).

Then test the reaction path:

```
React to any message in a channel the bot is in with :bookmark:
/research since:2026-04-01
```

You should see:
- Ephemeral ack: "🎧 On it — fetching your :bookmark: messages..."
- ~3-10 min later: a DM with an MP3 attached (or a markdown fallback if
  NotebookLM failed).

---

## Updating

```bash
uv run modal deploy main.py
```

Modal hot-swaps the webhook with zero downtime. Slack doesn't need to be touched
again.

## Checking logs

```bash
uv run modal app logs blok-digest-bot
```

Or view in the UI: https://modal.com/apps/<your-username>/main

## Tuning the prompts

The `prompts/daily_digest.md` is the most important file in this repo. Plan to
spend 12–15 hours tuning it against 5+ real days of your own Slack data. The
pattern:

1. Run `uv run modal run main.py::run_once --user-id U-YOUR-ID`
2. Read the DM it sent you critically. Which todos are wrong? Which real todos
   did it miss? Which themes felt off?
3. Edit `prompts/daily_digest.md` with sharper rules.
4. `uv run modal deploy main.py`
5. Repeat.

The citation validator in `validate.py` will silently drop any todo where Claude
hallucinated a permalink. Watch for that in the logs — if the `dropped_count` is
consistently >0, Claude is paraphrasing URLs and the prompt needs tightening.

---

## Known limitations (acknowledged in the design doc)

- **Messages bookmarked before install aren't indexed.** The reaction-added
  accumulator only captures reactions from install forward. Bookmarks from
  earlier don't appear in `/research` output.
- **@mentions only come from channels the bot is in.** If a user is @mentioned
  in a channel the bot isn't a member of, the digest misses it.
- **NotebookLM cookie rotation.** Google rotates session cookies roughly weekly.
  When `/research` starts failing with a `NotebookLMError`, re-run
  `notebooklm login` locally and refresh the `notebooklm-auth` Modal secret.
- **Prompt tuning is a you-shaped activity.** No amount of scaffolding replaces
  reading real digest output and iterating. Budget the full 12–15h.

---

## Tearing it down

```bash
uv run modal app stop blok-digest-bot
```

In the Slack app config, delete the app (Settings → Basic Information →
scroll to the bottom).
