#!/usr/bin/env python3
"""Connect the Kārya bot: run once after installing the Slack app.

    python3 ~/karya/setup_slack.py --from-clipboard   # copy the xoxb- token first
    python3 ~/karya/setup_slack.py                    # in a real terminal: asks, hidden

Checks the token with Slack, finds your user in that workspace, opens the DM,
sends a test message, and saves the token to config.json (readable only by
you, and gitignored). The token is never printed. With --from-clipboard the
clipboard is cleared once the token is saved.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

import slack_bot
from store import ROOT, load_config

CONFIG = Path(os.environ.get("KARYA_CONFIG", ROOT / "config.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect the Kārya Slack bot")
    parser.add_argument("--from-clipboard", action="store_true", help="read the xoxb- token from the clipboard")
    parser.add_argument("--email", help="your email in the bot's workspace (default: the one in config)")
    args = parser.parse_args()
    if args.from_clipboard:
        token = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    elif sys.stdin.isatty():
        token = getpass.getpass("Bot User OAuth Token (starts with xoxb-, input hidden): ").strip()
    else:
        print("No terminal to type into here. Copy the Bot User OAuth Token (xoxb-...), then run:\n"
              "  python3 ~/karya/setup_slack.py --from-clipboard")
        return 1
    if not token.startswith("xoxb-"):
        where = "The clipboard doesn't hold" if args.from_clipboard else "That isn't"
        print(f"{where} a bot token. Copy the one labelled 'Bot User OAuth Token' (it starts with xoxb-) and try again.")
        return 1
    try:
        who = slack_bot.call(token, "auth.test")
    except Exception as exc:  # noqa: BLE001
        print(f"Slack rejected the token: {exc}")
        return 1
    print(f"Token works: bot '{who.get('user')}' in workspace '{who.get('team')}'.")
    default_email = load_config().get("email", "")
    if args.email or not sys.stdin.isatty():
        email = args.email or default_email
        print(f"Looking you up as {email} (pass --email to use a different one).")
    else:
        email = input(f"Your email in that workspace [{default_email}]: ").strip() or default_email
    try:
        user = slack_bot.lookup_by_email(token, email)
        channel = slack_bot.open_dm(token, user["id"])
        slack_bot.post(token, channel, "Kārya is connected. Your plan for the day will arrive here every morning, "
                                       "and new urgent asks will ping you during the day.")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach you in that workspace: {exc}")
        print("Check the email matches your account there, and that the app has users:read.email and im:write.")
        return 1
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    config.update({"slack_bot_token": token, "slack_bot_user_id": user["id"], "slack_bot_dm_channel": channel,
                   "slack_bot_team": who.get("team")})
    # Create it private from the start, so the token is never briefly world-readable.
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(config, indent=2) + "\n")
    CONFIG.chmod(0o600)  # an existing file keeps its old mode through os.open
    if args.from_clipboard:
        subprocess.run(["pbcopy"], input="", text=True)  # don't leave the token sitting on the clipboard
    print(f"Saved. A test message is in your DM with the bot in '{who.get('team')}'. Check that your phone buzzed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
