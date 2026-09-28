"""Minimal Slack Web API client for the Kārya bot (stdlib only).

The bot lives in a private workspace of your own, not your work one:
reading asks still goes through the Slack connector in sync.py, and only the
reminders are written here. A message from a bot is what makes the phone buzz
(Slack never notifies you about your own messages), and chat.scheduleMessage
is what lets the 08:00 list arrive while the Mac is asleep: Slack holds it
and delivers it.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

API = "https://slack.com/api/"


class SlackError(RuntimeError):
    pass


def call(token: str, method: str, **params: Any) -> dict[str, Any]:
    """POST a Web API method. Raises SlackError when Slack says ok: false."""
    body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    request = urllib.request.Request(API + method, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())
    if not data.get("ok"):
        raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
    return data


def lookup_by_email(token: str, email: str) -> dict[str, Any]:
    # users.lookupByEmail only accepts form encoding, not JSON.
    request = urllib.request.Request(API + "users.lookupByEmail?" + urllib.parse.urlencode({"email": email}),
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())
    if not data.get("ok"):
        raise SlackError(f"users.lookupByEmail: {data.get('error', 'unknown error')}")
    return data["user"]


def open_dm(token: str, user_id: str) -> str:
    return call(token, "conversations.open", users=user_id)["channel"]["id"]


def post(token: str, channel: str, text: str) -> str:
    return call(token, "chat.postMessage", channel=channel, text=text, unfurl_links=False, unfurl_media=False)["ts"]


def schedule(token: str, channel: str, text: str, post_at: int) -> str:
    return call(token, "chat.scheduleMessage", channel=channel, text=text, post_at=int(post_at),
                unfurl_links=False, unfurl_media=False)["scheduled_message_id"]


def unschedule(token: str, channel: str, scheduled_id: str) -> None:
    try:
        call(token, "chat.deleteScheduledMessage", channel=channel, scheduled_message_id=scheduled_id)
    except SlackError as exc:
        # Already delivered, or already gone: either way nothing is left to replace.
        if "invalid_scheduled_message_id" not in str(exc):
            raise
