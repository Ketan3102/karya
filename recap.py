#!/usr/bin/env python3
"""Friday recap email: the work that got done this week, grouped by project
with the outcome noted at the time, plus what carries into next week.
Personal tasks are left out: this is the work record.

A record, not an alert, so email suits it: Gmail keeps it searchable. Sent
from your Gmail (to ``recap_to``, default your ``email``)
through the Gmail connector in a headless Claude run that may only call
send_message, with the exact arguments built here.

    python3 recap.py              # this week (Monday to today), once per week
    python3 recap.py --dry-run    # print the email, send nothing
    python3 recap.py --force      # send even if this week's already went
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date, timedelta
from typing import Any

import headless
from store import Store, load_config, today

DAY = "%a %d %b"


def week_bounds(on: date) -> tuple[date, date]:
    return on - timedelta(days=on.weekday()), on


def build(store: Store, start: date, end: date) -> dict[str, Any]:
    review = store.review(start.isoformat(), end.isoformat(), space="work")
    open_tasks = [t for t in store.list(limit=1000) if t.get("space", "work") == "work"]
    next_mon = end + timedelta(days=7 - end.weekday())
    next_week = [t for t in open_tasks if t["due_date"] and t["due_date"] <= (next_mon + timedelta(days=6)).isoformat()]
    next_week.sort(key=lambda t: t["due_date"])
    urgent = [t for t in open_tasks if t["priority"] == "urgent" and t not in next_week]
    subject = f"Kārya · week of {start:%d %b}: {review['count']} done, {len(open_tasks)} open"

    def item(t: dict[str, Any], extra: str = "") -> str:
        title = html.escape(t["title"])
        if t.get("source_url"):
            title = f'<a href="{html.escape(t["source_url"])}" style="color:#1f6f6b;text-decoration:none">{title}</a>'
        note = f'<div style="color:#57534c;font-size:13px;margin-top:2px">{html.escape(t["completion_note"])}</div>' if t.get("completion_note") else ""
        return f'<li style="margin:0 0 8px">{title}{extra}{note}</li>'

    parts = [f'<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#1c1b19;max-width:640px">',
             f'<h2 style="font-family:Georgia,serif;font-weight:600;margin:0 0 4px">Week of {start:%d %b}</h2>',
             f'<p style="color:#6c6861;margin:0 0 18px">{review["count"]} tasks finished, {start:{DAY}} to {end:{DAY}}. '
             f'{len(open_tasks)} still open.</p>']
    text = [subject, "", f"Finished {start:{DAY}} to {end:{DAY}}: {review['count']}", ""]
    for group in review["groups"]:
        parts.append(f'<h3 style="font-family:Georgia,serif;font-weight:600;margin:16px 0 6px">{html.escape(group["project"])} · {len(group["tasks"])}</h3><ul style="padding-left:18px;margin:0">')
        parts += [item(t) for t in group["tasks"]]
        parts.append("</ul>")
        text.append(f"{group['project']} ({len(group['tasks'])})")
        text += [f"  - {t['title']}" + (f": {t['completion_note']}" if t.get("completion_note") else "") for t in group["tasks"]]
        text.append("")
    if not review["groups"]:
        parts.append('<p style="color:#6c6861">Nothing marked done this week.</p>')
    if next_week:
        parts.append('<h3 style="font-family:Georgia,serif;font-weight:600;margin:22px 0 6px">Due by the end of next week</h3><ul style="padding-left:18px;margin:0">')
        parts += [item(t, f' <span style="color:#955e10">· due {date.fromisoformat(t["due_date"]):{DAY}}</span>') for t in next_week[:12]]
        parts.append("</ul>")
        text.append("Due by the end of next week:")
        text += [f"  - {t['title']} (due {t['due_date']})" for t in next_week[:12]]
        text.append("")
    if urgent:
        parts.append('<h3 style="font-family:Georgia,serif;font-weight:600;margin:22px 0 6px;color:#b4412e">Urgent, no deadline</h3><ul style="padding-left:18px;margin:0">')
        parts += [item(t) for t in urgent[:12]]
        parts.append("</ul>")
        text.append("Urgent, no deadline:")
        text += [f"  - {t['title']}" for t in urgent[:12]]
    parts.append('<p style="color:#6c6861;font-size:12px;margin-top:26px">Sent by Kārya. The full list for any period is on the board under Review.</p></div>')
    return {"subject": subject, "html": "".join(parts), "text": "\n".join(text), "count": review["count"]}


def send(config: dict[str, Any], to: str, subject: str, html_body: str, text_body: str) -> str:
    """Send through the Gmail connector; returns Gmail's result (the message id)."""
    args = {"to": [to], "subject": subject, "htmlBody": html_body, "body": text_body}
    prompt = ("Call the Gmail send_message tool exactly once with exactly these arguments, unchanged, "
              "then reply with the word sent. Do nothing else.\n" + json.dumps(args, ensure_ascii=False))
    return headless.run_tool(config, "mcp__claude_ai_Gmail__send_message", prompt)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the weekly Kārya recap email")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--test", action="store_true", help="send now marked [test]; Friday's real one still goes")
    args = parser.parse_args()
    store, config = Store(), load_config()
    start, end = week_bounds(today())
    built = build(store, start, end)
    if args.dry_run:
        print(built["subject"]); print(); print(built["text"]); return 0
    if args.test:
        built["subject"] = "[test] " + built["subject"]
    elif not args.force and store.kv_get("recap_week") == start.isoformat():
        print(f"recap for the week of {start} already sent"); return 0
    try:
        result = send(config, config.get("recap_to") or config["email"], built["subject"], built["html"], built["text"])
    except Exception as exc:  # noqa: BLE001
        store.record_notification("email", False, built["text"], None, str(exc))
        print(exc, file=sys.stderr)
        return 1
    if not args.test:
        store.kv_set("recap_week", start.isoformat())
    store.record_notification("email", True, built["text"], None)
    print(f"sent: {built['subject']}  ({result[:80]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
