"""Timed reminders: a Slack ping a few minutes before a task's due time.

When a task gets a clock time ("order dinner before 7pm"), the bot confirms
it straight away ("Reminder set… I'll ping you at 6:55 pm") and hands Slack a
scheduled message for due time minus ``alarm_lead_minutes`` (default 5).
Slack holds it and delivers it, so it fires even with the Mac asleep.

Changing the time replaces the scheduled message; finishing, dropping or
deleting the task, or clearing its time, cancels it. ``reconcile`` runs after
every sync and at login to repair anything missed (a time set before the bot
was connected, a Slack error). One row per task in the ``alarms`` table
remembers what is scheduled, until the due time has passed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import slack_bot
from store import Store, load_config, tz


def due_at(task: dict[str, Any]) -> datetime | None:
    if not task.get("due_date") or not task.get("due_time"):
        return None
    hh, mm = (int(x) for x in task["due_time"].split(":"))
    return datetime.fromisoformat(task["due_date"]).replace(hour=hh, minute=mm, tzinfo=tz())


def clock(when: datetime) -> str:
    return when.strftime("%-I:%M %p").lower()


def _when(due: datetime, now: datetime) -> str:
    if due.date() == now.date():
        return f"today at {clock(due)}"
    if due.date() == now.date() + timedelta(days=1):
        return f"tomorrow at {clock(due)}"
    return f"{due:%a %d %b} at {clock(due)}"


def _title(task: dict[str, Any]) -> str:
    url = task.get("source_url")
    return f"<{url}|{task['title']}>" if url else task["title"]


def on_change(store: Store, before: dict[str, Any] | None, after: dict[str, Any] | None, *,
              config: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    """Store listener. Returns what it did, for logs and tests."""
    config = config if config is not None else load_config()
    if not config.get("slack_bot_token"):
        return "no bot"
    token, channel = config["slack_bot_token"], config["slack_bot_dm_channel"]
    task = after or before
    if not task:
        return "nothing"
    now = now or datetime.now(tz())
    existing = store.alarm_get(task["id"])
    due = due_at(after) if after and after.get("open") else None

    if due is None or due <= now:
        if existing:
            if existing.get("scheduled_id"):
                slack_bot.unschedule(token, existing["channel"], existing["scheduled_id"])
            store.alarm_drop(task["id"])
            return "cancelled"
        return "no alarm"
    if existing and existing["due_at"] == due.isoformat():
        return "unchanged"
    if existing and existing.get("scheduled_id"):
        slack_bot.unschedule(token, existing["channel"], existing["scheduled_id"])

    lead = timedelta(minutes=int(config.get("alarm_lead_minutes", 5)))
    remind = due - lead
    if remind > now + timedelta(minutes=1):
        text = f"⏰ *{_title(after)}* needs to be done by {clock(due)}, in {int(lead.total_seconds() // 60)} minutes."
        scheduled = slack_bot.schedule(token, channel, text, int(remind.timestamp()))
        store.alarm_set(after["id"], scheduled, channel, int(remind.timestamp()), due.isoformat())
        verb = "moved" if existing else "set"
        slack_bot.post(token, channel, f"Reminder {verb}: *{_title(after)}*, due {_when(due, now)}. "
                                       f"I'll ping you at {clock(remind)}.")
        return f"scheduled for {clock(remind)}"
    # Less than the lead time left: remind now, once.
    minutes = max(1, int((due - now).total_seconds() // 60))
    slack_bot.post(token, channel, f"⏰ *{_title(after)}* is due at {clock(due)}, {minutes} minute{'s' if minutes != 1 else ''} from now.")
    store.alarm_set(after["id"], None, channel, int(now.timestamp()), due.isoformat())
    return "reminded now"


def reconcile(store: Store, *, config: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, int]:
    """Make Slack's schedule match the tasks: add missing alarms, cancel stale ones."""
    config = config if config is not None else load_config()
    out = {"checked": 0, "changed": 0, "cleared": 0}
    if not config.get("slack_bot_token"):
        return out
    now = now or datetime.now(tz())
    for task in store.list(limit=2000):
        if task.get("due_time"):
            out["checked"] += 1
            if on_change(store, None, task, config=config, now=now) not in ("unchanged", "no alarm"):
                out["changed"] += 1
    for row in store.alarms():
        task = store.get(row["task_id"])
        if task is None or not task["open"] or not task.get("due_time"):
            # Deleted, finished or no longer timed: cancel whatever Slack still holds.
            if row.get("scheduled_id"):
                slack_bot.unschedule(config["slack_bot_token"], row["channel"], row["scheduled_id"])
            store.alarm_drop(row["task_id"])
            out["cleared"] += 1
        elif datetime.fromisoformat(row["due_at"]) <= now:
            store.alarm_drop(row["task_id"])  # delivered and due passed: nothing left to track
            out["cleared"] += 1
    return out
