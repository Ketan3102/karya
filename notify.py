#!/usr/bin/env python3
"""Reminders: the morning plan and "new asks" pings in Slack, plus a macOS
banner.

With the Kārya bot connected (config ``slack_bot_token``, set up by
setup_slack.py in your own private workspace):
* every sync from 18:00 onwards hands Slack tomorrow's plan, scheduled for
  ``morning_time`` (08:00). Slack holds it and delivers it, so it arrives
  even if the Mac is asleep, and it comes from the bot, so the phone buzzes.
  A later evening run replaces it with a fresher list.
* a sync before the morning time schedules today's plan the same way; if
  the morning passed with nothing scheduled (the Mac was off all night),
  the first sync of the day sends the plan straight away.
* every other sync pings only when new asks have arrived since the last one.

Without the bot, the older behaviour stays: the full digest goes to an
incoming webhook if one is set, or to ``slack_channel_id`` through the Slack
connector - which posts as you, so it never notifies you. With none of
those set, reminders are macOS banners only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import headless
import slack_bot
from store import ROOT, Store, load_config, local_date_of, now_utc, today, tz

SOURCE_LABEL = {"slack": "Slack", "gmail": "Gmail", "linear": "Linear", "calendar": "Calendar",
                "claude": "Claude", "codex": "Codex", "manual": "Board"}


def _day(value: str | None) -> str:
    return datetime.fromisoformat(value).strftime("%a %d %b") if value else ""


def _clock(hhmm: str) -> str:
    return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p").lower()


def _line(task: dict[str, Any], style: str, *, show_due: bool = True) -> str:
    title = task["title"]
    url = task.get("source_url")
    if url:
        title = f"<{url}|{title}>" if style == "mrkdwn" else f"[{title}]({url})"
    bits = [SOURCE_LABEL.get(task["source"], task["source"].title())]
    if show_due and task.get("due_date"):
        bits.append(f"due {_day(task['due_date'])}" + (f" {_clock(task['due_time'])}" if task.get("due_time") else ""))
    elif task.get("due_time"):
        bits.append(f"by {_clock(task['due_time'])}")
    if task.get("requester"):
        bits.append(f"from {task['requester']}")
    if task["priority"] in ("urgent", "high"):
        bits.insert(0, task["priority"])
    return f"• {title} ({', '.join(bits)})"


def build(store: Store, style: str = "md", *, on: date | None = None, morning: bool = False,
          personal: bool = False) -> dict[str, Any]:
    """``personal`` adds a Personal section. Only the bot DM (your own
    workspace) asks for it; anything posted into a work workspace leaves personal
    tasks out entirely."""
    d = dict(store.digest(on=on))
    mine = [t for t in d["planned_today"] + d["overdue"] + d["due_today"] + d["due_soon"] + d["urgent"]
            if t.get("space") == "personal"]
    for key in ("overdue", "due_today", "due_soon", "urgent", "planned_today", "waiting"):
        d[key] = [t for t in d[key] if t.get("space", "work") == "work"]
    d["counts"] = {**d["counts"], **{k: len(d[k]) for k in ("overdue", "due_today", "due_soon", "urgent")}}
    last = store.last_notification()
    since = last["at"] if last else None
    all_open = store.list(limit=1000)
    new = [] if morning else [t for t in all_open if since and t["created_at"] > since and t["source"] in ("slack", "gmail", "linear", "calendar")
                              and t.get("space", "work") == "work"]
    shown = {t["id"] for t in d["overdue"] + d["due_today"] + d["due_soon"]}
    urgent = [t for t in d["urgent"] if t["id"] not in shown]
    shown |= {t["id"] for t in urgent}
    new = [t for t in new if t["id"] not in shown]
    planned = [t for t in d["planned_today"] if t["id"] not in shown]

    bold = (lambda s: f"*{s}*") if style == "mrkdwn" else (lambda s: f"**{s}**")
    board_url = f"http://127.0.0.1:{load_config()['port']}/"
    # Each count links to the board with that filter on (#urgent etc.), so a tap
    # from the digest lands on the same tasks, highlighted where they live.
    link = (lambda text, key: f"<{board_url}#{key}|{text}>") if style == "mrkdwn" else (lambda text, key: f"[{text}]({board_url}#{key})")
    counts = d["counts"]
    parts = [(f"{counts['overdue']} overdue", "overdue", counts["overdue"]),
             (f"{counts['due_today']} due today", "due_today", counts["due_today"]),
             (f"{counts['due_soon']} due in the next 2 days", "due_soon", counts["due_soon"]),
             (f"{counts['urgent']} urgent", "urgent", counts["urgent"]),
             (f"{len(new)} new", None, len(new))]
    head = [text for text, _, n in parts if n] or ["nothing due"]
    # The morning plan is read on the phone, where a link to the Mac's board goes nowhere.
    linked = head if morning else [link(text, key) if key else text for text, key, n in parts if n] or ["nothing due"]
    title = f"Kārya · {'your day, ' if morning else ''}{date.fromisoformat(d['today']).strftime('%a %d %b')}"
    lines = [f"{bold(title)} — {' · '.join(linked)}"]

    def section(name: str, tasks: list[dict[str, Any]], show_due: bool = True, cap: int = 8) -> None:
        if not tasks:
            return
        lines.append("")
        lines.append(bold(name))
        lines.extend(_line(t, style, show_due=show_due) for t in tasks[:cap])
        if len(tasks) > cap:
            lines.append(f"…and {len(tasks) - cap} more")

    section("Overdue", d["overdue"])
    section("Due today", d["due_today"], show_due=False)
    section("Due in the next 2 days", d["due_soon"])
    section("Urgent, no deadline set", urgent)
    section("New since the last reminder", new)
    section("On your plan" if morning else "Also planned for today", planned, cap=10 if morning else 6)
    if personal and mine:
        seen, own = set(), []
        for t in mine:
            if t["id"] not in seen:
                seen.add(t["id"]); own.append(t)
        section("Personal", own, cap=8)
    if d["waiting"]:
        lines.append("")
        lines.append(f"{len(d['waiting'])} waiting on someone else.")
    lines.append("")
    lines.append(f"Board (on your Mac): {board_url}")

    key_ids = sorted(t["id"] + (t.get("due_date") or "") for t in d["overdue"] + d["due_today"] + d["due_soon"] + urgent + new)
    fingerprint = hashlib.sha256(json.dumps([d["today"], key_ids]).encode()).hexdigest()[:16]
    banner = ", ".join(head)
    return {"text": "\n".join(lines), "fingerprint": fingerprint, "banner": banner, "digest": d, "new": new,
            "urgent": bool(d["overdue"] or d["due_today"] or urgent)}


def _macos(title: str, message: str) -> None:
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False, timeout=15, capture_output=True)


def _webhook(url: str, text: str) -> None:
    request = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status >= 300:
            raise RuntimeError(f"webhook returned {response.status}")


def _via_claude(config: dict[str, Any], channel: str, text: str) -> None:
    prompt = (f"Post the message between the markers to Slack channel {channel} using slack_send_message, "
              "exactly as written, then reply with the word posted. Do not do anything else.\n"
              f"<<<MESSAGE\n{text}\nMESSAGE>>>")
    headless.run_tool(config, "mcp__claude_ai_Slack__slack_send_message", prompt)


def send(store: Store, *, force: bool = False, dry_run: bool = False, reason: str = "") -> dict[str, Any]:
    config = load_config()
    style = "mrkdwn" if config.get("slack_webhook_url") else "md"
    built = build(store, style)
    last = store.last_notification()
    first_today = not last or local_date_of(last["at"]) != today().isoformat()
    if not force and not first_today and last and last.get("fingerprint") == built["fingerprint"]:
        return {"sent": False, "why": "nothing changed since the last reminder", **built}
    if dry_run:
        return {"sent": False, "why": "dry run", **built}

    results = {}
    if config.get("macos_notifications") and (built["urgent"] or built["new"] or first_today):
        _macos("Kārya", built["banner"])
        results["macos"] = "ok"
    try:
        if config.get("slack_webhook_url"):
            mention = f"<@{config['slack_user_id']}> " if config.get("slack_user_id") else ""
            _webhook(config["slack_webhook_url"], mention + built["text"])
            channel = "slack-webhook"
        elif config.get("slack_channel_id"):
            _via_claude(config, config["slack_channel_id"], built["text"])
            channel = "slack-channel"
        else:
            # No Slack at all: the banner was the reminder. Record it so the same digest isn't repeated.
            store.record_notification("macos", True, built["text"], built["fingerprint"])
            return {"sent": "macos" in results, "results": {**results, "slack": "not configured"}, "reason": reason, **built}
        store.record_notification(channel, True, built["text"], built["fingerprint"])
        results["slack"] = "ok"
    except Exception as exc:  # noqa: BLE001 - a failed post must not lose the reminder silently
        store.record_notification("slack", False, built["text"], built["fingerprint"], str(exc))
        results["slack"] = f"failed: {exc}"
        _macos("Kārya: Slack reminder failed", str(exc)[:120])
    return {"sent": results.get("slack") == "ok", "results": results, "reason": reason, **built}


# --------------------------------------------------------------- bot mode
def _post_at(when: datetime) -> int:
    return int(when.timestamp())


def _morning_target(config: dict[str, Any], now: datetime) -> datetime | None:
    """When the next morning plan should arrive, or None in the middle of the day."""
    hour, minute = (int(x) for x in str(config.get("morning_time", "08:00")).split(":"))
    todays = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < todays - timedelta(minutes=1):
        return todays
    if now.hour >= int(config.get("evening_hour", 18)):
        return todays + timedelta(days=1)
    return None


def morning(store: Store, config: dict[str, Any], now: datetime) -> str:
    token, channel = config["slack_bot_token"], config["slack_bot_dm_channel"]
    target = _morning_target(config, now)
    state = store.kv_get("morning", {}) or {}
    if target is None:
        # Midday. If no plan was scheduled or sent for today (the Mac slept
        # through the evening run), send today's plan now, once.
        if state.get("date") == now.date().isoformat():
            return "already delivered"
        built = build(store, "mrkdwn", morning=True, personal=True)
        slack_bot.post(token, channel, built["text"])
        store.kv_set("morning", {"date": now.date().isoformat(), "sent_at": now_utc()})
        store.record_notification("slack-bot", True, built["text"], built["fingerprint"])
        return "sent now (catch-up)"
    built = build(store, "mrkdwn", on=target.date(), morning=True, personal=True)
    if state.get("date") == target.date().isoformat() and state.get("id"):
        slack_bot.unschedule(token, channel, state["id"])
    scheduled_id = slack_bot.schedule(token, channel, built["text"], _post_at(target))
    store.kv_set("morning", {"date": target.date().isoformat(), "id": scheduled_id, "post_at": _post_at(target)})
    store.record_notification("slack-bot-scheduled", True, built["text"], built["fingerprint"])
    return f"scheduled for {target:%a %d %b %H:%M}"


def new_asks(store: Store, config: dict[str, Any]) -> str:
    """Ping about asks that arrived since the last ping. Silent when there are none."""
    since = store.kv_get("pinged_at")
    already = set(store.kv_get("pinged_ids", []))
    stamp = now_utc()
    # Timestamps are whole seconds, so count from `since` inclusively and skip
    # the tasks the previous ping already covered.
    fresh = [t for t in store.list(limit=1000)
             if since and t["created_at"] >= since and t["id"] not in already
             and t["source"] in ("slack", "gmail", "linear", "calendar")]
    store.kv_set("pinged_at", stamp)
    store.kv_set("pinged_ids", [t["id"] for t in fresh])
    if not since:
        return "first run: baseline set, nothing sent"
    if not fresh:
        return "no new asks"
    fresh.sort(key=lambda t: {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(t["priority"], 2))
    lines = [f"*Kārya · {len(fresh)} new ask{'s' if len(fresh) != 1 else ''}*"]
    lines += [_line(t, "mrkdwn") for t in fresh[:10]]
    if len(fresh) > 10:
        lines.append(f"…and {len(fresh) - 10} more on the board")
    text = "\n".join(lines)
    slack_bot.post(config["slack_bot_token"], config["slack_bot_dm_channel"], text)
    store.record_notification("slack-bot", True, text, None)
    if config.get("macos_notifications") and any(t["priority"] == "urgent" for t in fresh):
        _macos("Kārya", f"{len(fresh)} new, including urgent")
    return f"pinged about {len(fresh)}"


def after_sync(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """What sync.py calls when it finishes."""
    config = load_config()
    if not config.get("slack_bot_token"):
        result = send(store, reason="sync")
        return {"mode": "legacy", "result": result.get("why") or result.get("results")}
    now = now or datetime.now(tz())
    out: dict[str, Any] = {"mode": "bot"}
    for name, step in (("new_asks", new_asks), ("morning", morning)):
        try:
            out[name] = step(store, config) if name == "new_asks" else step(store, config, now)
        except Exception as exc:  # noqa: BLE001 - one failed step must not hide the other
            out[name] = f"failed: {exc}"
            store.record_notification("slack-bot", False, "", None, f"{name}: {exc}")
            _macos("Kārya: Slack reminder failed", str(exc)[:120])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the Kārya reminder digest")
    parser.add_argument("--force", action="store_true", help="send even if nothing changed")
    parser.add_argument("--dry-run", action="store_true", help="print the digest, send nothing")
    parser.add_argument("--morning", metavar="DAY", help="print the morning plan for a day (e.g. tomorrow), send nothing")
    parser.add_argument("--test-schedule", action="store_true",
                        help="ask Slack to deliver a test DM in 2 minutes (close the lid and see it arrive)")
    args = parser.parse_args()
    if args.test_schedule:
        config = load_config()
        if not config.get("slack_bot_token"):
            sys.exit("The bot isn't connected yet: run setup_slack.py first.")
        when = datetime.now(tz()) + timedelta(minutes=2)
        slack_bot.schedule(config["slack_bot_token"], config["slack_bot_dm_channel"],
                           f"Kārya test: scheduled at {datetime.now(tz()):%H:%M}, delivered by Slack at {when:%H:%M}.", _post_at(when))
        print(f"Scheduled for {when:%H:%M}. Close the lid now; it should still arrive on your phone.")
        return
    if args.morning:
        from store import parse_day
        print(build(Store(), "mrkdwn", on=date.fromisoformat(parse_day(args.morning)), morning=True, personal=True)["text"])
        return
    result = send(Store(), force=args.force, dry_run=args.dry_run, reason="manual")
    print(result["text"])
    print(f"\n-> {result.get('why') or result.get('results')}", file=sys.stderr)


if __name__ == "__main__":
    main()
