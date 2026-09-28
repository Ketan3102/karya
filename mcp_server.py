#!/usr/bin/env python3
"""Kārya MCP server (stdio, no dependencies).

Registered at user scope, so every Claude Code / Codex session on this machine
can record work as it happens: start a task when real work begins, log
decisions, complete it with an outcome, and pull the day's plan.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from datetime import timedelta

import alarms
from search import HL_CLOSE, HL_OPEN
from store import OPEN, PRIORITIES, Store, load_config, today

CONFIG = load_config()
WHO = CONFIG.get("name") or "the user"
EXAMPLES = {"title": "Fix the login redirect loop", "project": "Acme, Website, Kārya, Home",
            "ref": "ENG-2391", **(CONFIG.get("examples") or {})}

INSTRUCTIONS = f"""Kārya is {WHO}'s task tracker. Keep it current without being asked:
- When {WHO} asks for a piece of real work (build, fix, investigate, write, review, run), call karya_start_task first. Skip purely conversational questions.
- The work may already be tracked (a Linear issue, a Slack ask, an earlier session). karya_start_task checks: if it finds similar open tasks it creates nothing and returns them as candidates. If one is the same work, call karya_start_task again with its task_id: that task is marked in progress and gets your updates. Only if none is, call again with new_task: true.
- If one piece of work covers several tracked tasks (one PR for three Linear issues), start the main one and karya_log the others with the same progress. Never create a task for work that already has one.
- When something is decided, blocked or waiting on someone, call karya_log (status "waiting" with waiting_on when blocked on a person).
- When the work is finished, call karya_complete_task with a one or two sentence outcome: what changed and why it mattered, with numbers if there are any.
- Set space "personal" for anything that is not {WHO}'s job: money, health, family, errands, side projects, learning for themselves. Personal tasks are kept out of the work view and the weekly work summary. When unsure, it is work.
- When {WHO} mentions a future to-do or deadline ("remind me", "by Friday", "next week"), call karya_add_task with due_date.
- When they give a clock time ("order dinner before 7pm", "call Sam at 4:30"), also pass due_time as HH:MM (24-hour). Kārya then pings them five minutes before it. Only when a time is actually stated.
- Use karya_digest when they ask what is pending, due or overdue.
- When they (or someone asking them) want to know whether something was done, when, or where, call karya_search before answering. It searches titles, outcomes, notes and every log entry. Search for the specific things involved (ticket or PR numbers, Linear IDs, codes, names) and try a second phrasing if the first finds nothing. Answer with the status, the date, and the outcome it records."""

DAY = {"type": "string", "description": "YYYY-MM-DD, or today / tomorrow / friday / next week / +3d"}
TIME = {"type": "string", "description": "Clock time the task must be done by, HH:MM 24-hour (e.g. 19:00). Only when a time is stated; a reminder fires 5 minutes before."}
PRIORITY = {"type": "string", "enum": list(PRIORITIES)}
SPACE = {"type": "string", "enum": ["work", "personal"], "description": "personal = not work (money, health, family, errands, side projects). Default work."}
TOOLS: list[dict[str, Any]] = [
    {"name": "karya_start_task",
     "description": "Record that work on something has started (status doing, planned today). Call this when the user asks for real work. Pass task_id to start an existing task instead of creating one. Returns the task and possible_duplicates.",
     "inputSchema": {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string", "description": f"Short imperative title, e.g. '{EXAMPLES['title']}'"},
         "task_id": {"type": "string", "description": "Continue this existing task instead of creating one"},
         "new_task": {"type": "boolean", "description": "Create a new task even though similar open tasks exist. Only after checking the candidates."},
         "notes": {"type": "string"},
         "project": {"type": "string", "description": f"Client, product or area, e.g. {EXAMPLES['project']}"},
         "due_date": DAY, "due_time": TIME, "priority": PRIORITY,
         "source_ref": {"type": "string", "description": f"Stable reference if the ask came from Linear/Slack, e.g. {EXAMPLES['ref']}"},
         "source_url": {"type": "string"}, "requester": {"type": "string"}, "space": SPACE}}},
    {"name": "karya_add_task",
     "description": "Add a to-do for later without starting it. Tasks with a due_date are planned on that day; tasks without one go to the Inbox.",
     "inputSchema": {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string"}, "notes": {"type": "string"}, "project": {"type": "string"},
         "new_task": {"type": "boolean", "description": "Create even though similar open tasks exist"},
         "due_date": DAY, "due_time": TIME, "planned_date": DAY, "priority": PRIORITY, "requester": {"type": "string"},
         "source": {"type": "string", "description": "slack, gmail, linear, calendar, or leave empty"},
         "source_ref": {"type": "string"}, "source_url": {"type": "string"}, "space": SPACE}}},
    {"name": "karya_log",
     "description": "Record progress, a decision or a blocker on a task. Set status 'waiting' with waiting_on when blocked on someone.",
     "inputSchema": {"type": "object", "required": ["task_id", "text"], "properties": {
         "task_id": {"type": "string"}, "text": {"type": "string"},
         "status": {"type": "string", "enum": list(OPEN)}, "waiting_on": {"type": "string"}}}},
    {"name": "karya_complete_task",
     "description": "Mark a task done. summary is the outcome in one or two sentences: what changed and why it mattered.",
     "inputSchema": {"type": "object", "required": ["task_id", "summary"], "properties": {
         "task_id": {"type": "string"}, "summary": {"type": "string"}}}},
    {"name": "karya_update_task",
     "description": "Change a task's fields: title, notes, project, priority, due_date, planned_date (null moves it to the Inbox), status (todo/doing/waiting/done/dropped).",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"}, "title": {"type": "string"}, "notes": {"type": "string"},
         "project": {"type": "string"}, "priority": PRIORITY, "due_date": DAY, "due_time": TIME, "planned_date": DAY,
         "status": {"type": "string", "enum": ["todo", "doing", "waiting", "done", "dropped"]},
         "waiting_on": {"type": "string"}, "space": SPACE, "note": {"type": "string", "description": "Why it changed"}}}},
    {"name": "karya_merge_tasks",
     "description": "Merge a duplicate into the task to keep: references, history, notes, link, deadline and progress carry over and the duplicate is dropped. Two tasks tracking different Linear issues cannot be merged.",
     "inputSchema": {"type": "object", "required": ["keep_id", "drop_id"], "properties": {
         "keep_id": {"type": "string"}, "drop_id": {"type": "string"}}}},
    {"name": "karya_list",
     "description": "List tasks for a view. Views: today, overdue, upcoming (next 7 days), inbox, doing, waiting, done_today, done_week, open. Optional query filters by text.",
     "inputSchema": {"type": "object", "properties": {
         "view": {"type": "string", "enum": ["today", "overdue", "upcoming", "inbox", "doing", "waiting", "done_today", "done_week", "open"]},
         "query": {"type": "string"}, "space": SPACE}}},
    {"name": "karya_search",
     "description": "Find tasks, done or not, by anything recorded about them: title, outcome, notes, every progress log (with dates), Linear IDs, project, requester. Keyword search, ranked; typos are corrected. Use it to answer 'did we do X / when / where'. Returns each task's status, done date, outcome, and the text that matched.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "Words, codes or IDs, e.g. 'login redirect', 'ENG-2430', 'PR 7040', 'v2.3.1'. \"Quoted words\" must appear together."},
         "status": {"type": "string", "enum": ["all", "open", "done"], "description": "Default all. done includes dropped."},
         "limit": {"type": "integer", "description": "Default 10"}}}},
    {"name": "karya_digest",
     "description": "What needs attention now: overdue, due today, due in two days, planned today, waiting, in progress, and done today.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "karya_ingest",
     "description": "Bulk upsert tasks found in Slack, Gmail, Linear or Calendar, keyed by (source, source_ref) so repeats never duplicate. completed closes tasks whose source says they are finished.",
     "inputSchema": {"type": "object", "properties": {
         "items": {"type": "array", "items": {"type": "object", "required": ["source", "source_ref", "title"], "properties": {
             "source": {"type": "string"}, "source_ref": {"type": "string"}, "title": {"type": "string"},
             "due_date": {"type": "string"}, "priority": PRIORITY, "requester": {"type": "string"},
             "source_url": {"type": "string"}, "notes": {"type": "string"}, "project": {"type": "string"},
             "reason": {"type": "string"}}}},
         "completed": {"type": "array", "items": {"type": "object", "required": ["source", "source_ref"], "properties": {
             "source": {"type": "string"}, "source_ref": {"type": "string"}, "summary": {"type": "string"}}}}}}},
]


def workspace() -> str:
    """ · repo, branch X: where the work happens. The MCP server runs in the
    session's own directory, so this answers "where did I do that?" later."""
    cwd = Path.cwd()
    if cwd in (Path.home(), Path("/")):
        return ""
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        top, branch = out.stdout.splitlines()[:2] if out.returncode == 0 else (str(cwd), "")  # paths may hold spaces
    except (OSError, ValueError, subprocess.SubprocessError):
        top, branch = str(cwd), ""
    return f" · {Path(top).name}" + (f", branch {branch}" if branch and branch != "HEAD" else "")


def brief(task: dict[str, Any] | None) -> dict[str, Any] | None:
    """The fields an agent needs, without the bookkeeping columns."""
    if not task:
        return None
    keep = ("id", "title", "status", "space", "priority", "due_date", "due_time", "planned_date", "project", "source", "source_ref",
            "source_url", "requester", "waiting_on", "completion_note", "match")
    return {k: task[k] for k in keep if task.get(k) not in (None, "")}


class Karya:
    def __init__(self, store: Store):
        self.store = store
        self.client = "agent"

    def _actor(self) -> str:
        name = self.client.lower()
        return "codex" if "codex" in name else "claude" if "claude" in name else name or "agent"

    def call(self, name: str, a: dict[str, Any]) -> Any:
        s, actor = self.store, self._actor()
        if name == "karya_start_task":
            task, how = self._existing(a)
            if task is None and not a.get("new_task"):
                cands = s.candidates(a["title"], a.get("notes") or "", floor=0.15)
                if cands:
                    return self._choose(cands)
            created = task is None
            if created:
                fields = {k: a[k] for k in ("notes", "project", "due_date", "due_time", "priority", "source_url", "requester", "space") if a.get(k)}
                task = s.create(a["title"], actor=actor, source=actor, source_ref=a.get("source_ref"), status="doing",
                                note=f"Started in {actor}{workspace()}", **fields)
            else:
                patch = {"status": "doing"} if task["status"] != "doing" else {}
                where = workspace()
                task = s.update(task["id"], patch, actor=actor, note=f"Started in {actor}" + (f" ({how})" if how else "") + where, kind="started") if patch else \
                    s.log(task["id"], f"Picked up again in {actor}" + (f" ({how})" if how else "") + where, actor=actor)
            return {"task": brief(task), "created": created, "matched_by": how}
        if name == "karya_add_task":
            source = (a.get("source") or actor).lower()
            existing, how = self._existing({**a, "source": source})
            if existing:
                return {"task": brief(existing), "created": False, "matched_by": how, "note": "already tracked; update it instead"}
            if not a.get("new_task"):
                cands = s.candidates(a["title"], a.get("notes") or "", floor=0.15)
                if cands:
                    return self._choose(cands)
            fields = {k: a[k] for k in ("notes", "project", "due_date", "due_time", "planned_date", "priority", "requester", "source_url", "space") if a.get(k)}
            task = s.create(a["title"], actor=actor, source=source, source_ref=a.get("source_ref"), **fields)
            return {"task": brief(task), "created": True}
        if name == "karya_log":
            patch = {k: a[k] for k in ("status", "waiting_on") if a.get(k)}
            return {"task": brief(s.log(a["task_id"], a["text"], actor=actor, kind="progress" if not patch else "updated", patch=patch))}
        if name == "karya_complete_task":
            return {"task": brief(s.complete(a["task_id"], a["summary"], actor=actor))}
        if name == "karya_update_task":
            patch = {k: v for k, v in a.items() if k not in ("task_id", "note")}
            if "planned_date" in patch and patch["planned_date"] in (None, ""):
                patch["planned_date"] = None
            return {"task": brief(s.update(a["task_id"], patch, actor=actor, note=a.get("note")))}
        if name == "karya_merge_tasks":
            return {"task": brief(s.merge(a["keep_id"], a["drop_id"], actor=actor))}
        if name == "karya_list":
            tasks = self._view(a.get("view") or "today", a.get("query"))
            if a.get("space"):
                tasks = [t for t in tasks if t["space"] == a["space"]]
            return {"tasks": [brief(t) for t in tasks]}
        if name == "karya_search":
            found = s.search(a["query"], status=a.get("status") or "all", limit=int(a.get("limit") or 10))
            plain = lambda text: (text or "").replace(HL_OPEN, "**").replace(HL_CLOSE, "**")  # noqa: E731
            tasks = []
            for t in found["tasks"]:
                item = dict(brief(t) or {}, done_date=t["done_date"], created=(t["created_at"] or "")[:10],
                            refs=t["refs"] or None, partial_match=t["partial"] or None)
                if t["hit"]:
                    item["matched"] = f"{t['hit']['label']}{' ' + t['hit']['date'] if t['hit']['date'] else ''}: {plain(t['hit']['text'])}"
                if t["context"] and t["context"]["text"] != t.get("completion_note"):
                    item["latest"] = f"{t['context']['label']}{' ' + t['context']['date'] if t['context']['date'] else ''}: {t['context']['text'][:400]}"
                tasks.append({k: v for k, v in item.items() if v not in (None, "", [])})
            out = {"total": found["total"], "counts": found["counts"], "tasks": tasks}
            if found["corrected"]:
                out["corrected"] = found["corrected"]
            if found["missing"]:
                out["not_found_anywhere"] = found["missing"]
            return out
        if name == "karya_digest":
            d = s.digest()
            return {"today": d["today"], "counts": d["counts"],
                    **{k: [brief(t) for t in d[k]] for k in ("overdue", "due_today", "due_soon", "planned_today", "doing", "waiting", "done_today")}}
        if name == "karya_ingest":
            outcome = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "closed": 0, "errors": []}
            for item in a.get("items") or []:
                try:
                    _, result = s.ingest(item, actor=actor)
                    outcome[result] += 1
                except (ValueError, KeyError) as exc:
                    outcome["errors"].append(f"{item.get('source_ref')}: {exc}")
            for item in a.get("completed") or []:
                if s.close_from_source(item["source"], item["source_ref"], item.get("summary") or "Closed at the source", actor=actor):
                    outcome["closed"] += 1
            return outcome
        raise ValueError(f"unknown tool {name}")

    def _existing(self, a: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """A task that certainly is this work: named by id, by reference, by a
        Linear ID written in the title or notes, or by an identical open title."""
        s = self.store
        if a.get("task_id"):
            return s.require(a["task_id"]), None
        if a.get("source_ref"):
            task = s.by_source(a.get("source") or "", a["source_ref"]) or s.by_ref(a["source_ref"])
            if task:
                return task, f"same reference {a['source_ref']}"
        task = s.by_mention(a.get("title") or "", a.get("notes") or "")
        if task:
            return task, f"mentions {task['source_ref']}"
        task = s.find_open_by_title(a.get("title") or "")
        return (task, "same title") if task else (None, None)

    @staticmethod
    def _choose(cands: list[dict[str, Any]]) -> dict[str, Any]:
        return {"created": False, "decision_needed": True,
                "candidates": [dict(brief(c) or {}, shared=c.get("shared")) for c in cands],
                "next": ("Nothing was created. If one of these is the same work, call this tool again with its task_id "
                         "(it is marked in progress and keeps its history). If none is, call again with new_task: true.")}

    def _view(self, view: str, query: str | None) -> list[dict[str, Any]]:
        s = self.store
        s.rollover()
        t0 = today().isoformat()
        if view in ("done_today", "done_week"):
            start = t0 if view == "done_today" else (today() - timedelta(days=6)).isoformat()
            tasks = s.done_between(start, t0)
        else:
            tasks = s.list(limit=1000)
            week = (today() + timedelta(days=7)).isoformat()
            pick = {
                "today": lambda t: t["planned_date"] == t0,
                "overdue": lambda t: bool(t["due_date"]) and t["due_date"] < t0,
                "upcoming": lambda t: bool(t["planned_date"]) and t0 < t["planned_date"] <= week,
                "inbox": lambda t: t["planned_date"] is None,
                "doing": lambda t: t["status"] == "doing",
                "waiting": lambda t: t["status"] == "waiting",
                "open": lambda t: True,
            }.get(view, lambda t: True)
            tasks = [t for t in tasks if pick(t)]
        if query:
            q = query.lower()
            tasks = [t for t in tasks if q in json.dumps(t, ensure_ascii=False).lower()]
        return tasks


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    store = Store()
    store.listeners.append(alarms.on_change)  # timed tasks get their Slack reminder
    karya = Karya(store)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        method, rid = request.get("method"), request.get("id")
        if rid is None:  # notification
            continue
        params = request.get("params") or {}
        try:
            if method == "initialize":
                karya.client = (params.get("clientInfo") or {}).get("name", "agent")
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "karya", "version": "2.0.0"},
                    "instructions": INSTRUCTIONS}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                try:
                    value = karya.call(params.get("name", ""), params.get("arguments") or {})
                    send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}})
                except (ValueError, KeyError) as exc:
                    # Tool errors go back as results so the agent can read and correct them.
                    send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
        except Exception as exc:  # noqa: BLE001 - never let one bad call kill the server
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(exc)}})


if __name__ == "__main__":
    main()
