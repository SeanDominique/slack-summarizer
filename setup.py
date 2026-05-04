#!/usr/bin/env python3
"""Interactive setup for Blok Digest Bot.

Walks you through every one-time install step:
    1. Verify prereqs (uv, modal CLI, modal auth, NotebookLM auth)
    2. Anthropic API key  → Modal secret `anthropic`
    3. NotebookLM cookie  → Modal secret `notebooklm-auth` (base64-encoded)
    4. Stub Slack secret  → Modal secret `slack-creds` (placeholder values, just
       so the first deploy can validate)
    5. `modal deploy main.py` → captures the public webhook URL
    6. Writes `generated_manifest.yaml` with the real URL filled in
    7. Prompts you to create the Slack App from the generated manifest
    8. Prompts for the real Slack bot token + signing secret → updates secret
    9. Redeploys so the warm container picks up the real signing secret
   10. Prints the final Slack-UI steps (event subscriptions + reinstall)

Run with: uv run python setup.py
"""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------- Pretty output ----------

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(s: str, color: str) -> str:
    return f"{color}{s}{RESET}" if USE_COLOR else s


def header(s: str) -> None:
    print()
    print(_c(s, BOLD))
    print(_c("─" * len(s), BOLD))


def ok(s: str) -> None:
    print(_c(f"  OK  {s}", GREEN))


def warn(s: str) -> None:
    print(_c(f"  !!  {s}", YELLOW))


def err(s: str) -> None:
    print(_c(f"  XX  {s}", RED))


def info(s: str) -> None:
    print(f"      {s}")


def ask(s: str) -> str:
    return input(_c(f"  ?  {s}: ", BOLD)).strip()


def ask_secret(s: str) -> str:
    return getpass.getpass(_c(f"  ?  {s}: ", BOLD)).strip()


def confirm(prompt: str) -> bool:
    while True:
        r = input(_c(f"  ?  {prompt} [y/n]: ", BOLD)).strip().lower()
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False


# ---------- Subprocess helpers ----------

def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """List-form subprocess. No shell, no history leakage of secret args."""
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env,
    )


def have(cmd: str) -> str | None:
    return shutil.which(cmd)


# ---------- Prereq checks ----------

def check_prereqs() -> None:
    header("Checking prereqs")
    if have("uv") is None:
        err("uv is not installed.")
        info("Install: curl -LsSf https://astral.sh/uv/install.sh | sh")
        sys.exit(1)
    ok(f"uv: {have('uv')}")

    # Modal CLI via uv (so we don't depend on a global install)
    r = run(["uv", "run", "modal", "--version"], check=False, capture=True)
    if r.returncode != 0:
        err("modal CLI not available via uv.")
        info("Run: uv sync   (this also installs modal from pyproject.toml)")
        sys.exit(1)
    ok(f"modal: {r.stdout.strip()}")

    # Modal auth — check ~/.modal.toml exists with a token
    modal_toml = Path.home() / ".modal.toml"
    if not modal_toml.exists():
        err("Modal account not authenticated.")
        info("Run: uv run modal setup    (then re-run this script)")
        sys.exit(1)
    contents = modal_toml.read_text()
    if "token_id" not in contents or "token_secret" not in contents:
        err("Modal token missing in ~/.modal.toml")
        info("Run: uv run modal setup    (then re-run this script)")
        sys.exit(1)
    ok("Modal auth: ready (~/.modal.toml found)")

    # NotebookLM auth — produced by `notebooklm login`
    auth_path = Path.home() / ".notebooklm" / "storage_state.json"
    if not auth_path.exists():
        err(f"NotebookLM auth not found at {auth_path}")
        info("Run: uv run notebooklm login    (opens a browser; sign into Google)")
        info("Then: uv run notebooklm auth check --test")
        info("Then re-run this script.")
        sys.exit(1)
    ok(f"NotebookLM auth: {auth_path}")


# ---------- Modal secret creation ----------

def secret_exists(name: str) -> bool:
    r = run(["uv", "run", "modal", "secret", "list"], check=False, capture=True)
    if r.returncode != 0:
        return False
    return any(
        line.strip().startswith("│ ") and name in line
        for line in r.stdout.splitlines()
    )


def create_or_update_secret(name: str, kvs: dict[str, str]) -> None:
    """Create a Modal secret. If it exists, replace it via --force."""
    cmd = ["uv", "run", "modal", "secret", "create", name]
    for k, v in kvs.items():
        cmd.append(f"{k}={v}")
    cmd.append("--force")
    r = run(cmd, check=False, capture=True)
    if r.returncode != 0:
        err(f"Failed to create secret {name!r}")
        info(r.stderr.strip())
        sys.exit(1)
    ok(f"Modal secret created: {name}")


# ---------- Steps ----------

def step_anthropic() -> None:
    header("Step 1/4 — Anthropic API key")
    info("Get one at https://console.anthropic.com/settings/keys")
    key = ask_secret("Anthropic API key (starts with sk-ant-)")
    if not key.startswith("sk-ant-"):
        warn("That doesn't look like an Anthropic key. Continuing anyway.")
    create_or_update_secret("anthropic", {"ANTHROPIC_API_KEY": key})


def step_notebooklm() -> None:
    header("Step 2/4 — NotebookLM cookie")
    auth_path = Path.home() / ".notebooklm" / "storage_state.json"
    encoded = base64.b64encode(auth_path.read_bytes()).decode()
    create_or_update_secret(
        "notebooklm-auth", {"NOTEBOOKLM_STORAGE_STATE_B64": encoded}
    )


def step_stub_slack() -> None:
    header("Step 3/4 — Stub Slack credentials (temporary)")
    if secret_exists("slack-creds"):
        warn("A 'slack-creds' Modal secret already exists.")
        info("If you're re-running this script after a previous successful setup,")
        info("answer 'n' here so we don't clobber your real tokens.")
        if not confirm("Overwrite slack-creds with placeholder values?"):
            info("Skipping. Re-using your existing slack-creds.")
            return
    info("Creating a placeholder slack-creds secret so the first deploy can")
    info("validate. We replace these with real tokens after deploy.")
    create_or_update_secret(
        "slack-creds",
        {"SLACK_BOT_TOKEN": "xoxb-stub", "SLACK_SIGNING_SECRET": "stub"},
    )


URL_RE = re.compile(r"https://[A-Za-z0-9._-]+--blok-digest-bot[A-Za-z0-9._-]*\.modal\.run")


def step_deploy_first() -> str | None:
    header("Step 4/4 — First deploy (this builds the Modal image; can take 1-3 min)")
    info("Running: uv run modal deploy main.py")
    r = run(["uv", "run", "modal", "deploy", "main.py"], check=False, capture=True)
    print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    if r.returncode != 0:
        err("Deploy failed. See output above.")
        sys.exit(1)
    combined = r.stdout + "\n" + r.stderr
    matches = URL_RE.findall(combined)
    # Webhook URL contains 'slack-webhook' (the asgi function name)
    webhook_urls = [u for u in matches if "slack-webhook" in u]
    url = (webhook_urls or matches or [None])[0]
    if url:
        ok(f"Webhook URL: {url}")
    else:
        warn("Couldn't auto-detect webhook URL.")
        info("Find it via: uv run modal app list")
        info("And paste it manually below.")
        url = ask("Paste the slack_webhook URL (e.g. https://...modal.run)")
    return url


def write_generated_manifest(webhook_url: str) -> Path:
    src = Path(__file__).parent / "slack_app_manifest.yaml"
    dst = Path(__file__).parent / "generated_manifest.yaml"
    text = src.read_text()
    text = text.replace("REPLACE_WITH_MODAL_URL", webhook_url.replace("https://", ""))
    dst.write_text(text)
    return dst


def step_create_slack_app(generated_path: Path) -> None:
    header("Step 5/7 — Create the Slack App in your workspace")
    info(f"A customized manifest has been written to: {generated_path}")
    print()
    info("In a browser:")
    info("  1. Open https://api.slack.com/apps")
    info("  2. Click 'Create New App' → 'From a manifest'")
    info("  3. Pick your workspace (where the bot will live)")
    info(f"  4. Paste the entire contents of {generated_path.name}")
    info("  5. Confirm and create the app")
    print()
    info("After the app is created:")
    info("  6. Sidebar → 'OAuth & Permissions' → click 'Install to Workspace' → Allow")
    info("  7. Copy the 'Bot User OAuth Token' (starts with xoxb-)")
    info("  8. Sidebar → 'Basic Information' → scroll to 'App Credentials'")
    info("  9. Click 'Show' next to 'Signing Secret', copy it")
    print()
    if not confirm("Done? Ready to paste the tokens"):
        info("OK — re-run this script when ready.")
        sys.exit(0)


def step_real_slack_secret() -> None:
    header("Step 6/7 — Replace stub Slack secret with real tokens")
    bot = ask_secret("Bot User OAuth Token (xoxb-...)")
    if not bot.startswith("xoxb-"):
        warn("That doesn't look like a bot token. Continuing anyway.")
    signing = ask_secret("Signing Secret")
    if len(signing) < 16:
        warn("Signing secret looks short. Continuing anyway.")
    create_or_update_secret(
        "slack-creds",
        {"SLACK_BOT_TOKEN": bot, "SLACK_SIGNING_SECRET": signing},
    )


def step_redeploy() -> None:
    header("Step 7/7 — Redeploy so the warm container picks up real Slack credentials")
    r = run(["uv", "run", "modal", "deploy", "main.py"], check=False, capture=True)
    print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    if r.returncode != 0:
        err("Redeploy failed.")
        sys.exit(1)
    ok("Redeploy complete.")


def final_instructions(webhook_url: str) -> None:
    events = f"{webhook_url}/slack/events"
    header("Final Slack config (manual UI steps)")
    info(_c("In the Slack App config (https://api.slack.com/apps → your app):", BOLD))
    info("")
    info(f"{_c('A.', BOLD)} Sidebar → 'Slash Commands' → edit /digest:")
    info(f"     Request URL: {_c(events, GREEN)}")
    info(f"     Save. Repeat for /research.")
    info("")
    info(f"{_c('B.', BOLD)} Sidebar → 'Event Subscriptions' → toggle ON:")
    info(f"     Request URL: {_c(events, GREEN)}")
    info(f"     Slack will send a challenge — should turn green within 5s.")
    info("")
    info(f"{_c('C.', BOLD)} Same page → 'Subscribe to bot events' → 'Add Bot User Event'")
    info(f"     Add each:")
    info(f"       reaction_added")
    info(f"       reaction_removed")
    info(f"       message.channels")
    info(f"       message.groups")
    info(f"       message.im")
    info(f"       message.mpim")
    info(f"       app_mention")
    info(f"     Save Changes (yellow banner at the top).")
    info("")
    info(f"{_c('D.', BOLD)} Sidebar → 'OAuth & Permissions' → 'Reinstall to Workspace'")
    info(f"     This picks up the new scopes from the events you just added.")
    info("")
    info(f"{_c('E.', BOLD)} Test in Slack:")
    info(f"     /digest on")
    info(f"     /digest")
    info(f"     React to any message with :bookmark:")
    info(f"     /research since:2026-04-01")
    info("")
    info(f"Logs: uv run modal app logs blok-digest-bot")
    info(f"Or: https://modal.com/apps")


# ---------- Argparse / main ----------

def main() -> None:
    parser = argparse.ArgumentParser(description="Blok Digest Bot — interactive setup")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only run the prereq check; do not create secrets or deploy.",
    )
    args = parser.parse_args()

    print(_c("Blok Digest Bot — Setup", BOLD))
    info("This walks through Anthropic + NotebookLM + Slack credentials, then deploys.")
    info("It will prompt for tokens you paste from the various consoles.")
    info("Tokens go into Modal Secrets — never written to disk in this repo.")

    check_prereqs()
    if args.check_only:
        ok("All prereqs satisfied.")
        return

    step_anthropic()
    step_notebooklm()
    step_stub_slack()
    webhook_url = step_deploy_first()
    if not webhook_url:
        err("No webhook URL — cannot continue.")
        sys.exit(1)

    generated = write_generated_manifest(webhook_url)
    step_create_slack_app(generated)
    step_real_slack_secret()
    step_redeploy()
    final_instructions(webhook_url)
    print()
    ok("Setup complete. Test in Slack and check Modal logs if anything looks off.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("Aborted.")
        sys.exit(130)
