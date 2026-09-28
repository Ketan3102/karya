"""Kārya's task store: one SQLite file shared by the web app, the MCP server
and the scheduled sync.

Three dates matter for a task and they are kept apart on purpose:

* ``due_date``     - the deadline someone else set ("by Friday").
* ``planned_date`` - the day you intend to do it. This is what the day
  columns show. ``NULL`` means the task sits in the Inbox, unplanned.
* ``completed_at`` - when it was actually finished. Done tasks are shown on
  the day they were completed, which is what makes the board a history.

All calendar dates are local: KARYA_TZ, else ``timezone`` in config.json,
else the Mac's own time zone. Timestamps are stored in UTC ISO-8601. Mixing
the two is how "due today" turns into "overdue" at 05:30 in the morning.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATUSES = ("todo", "doing", "waiting", "done", "dropped")
OPEN = ("todo", "doing", "waiting")
PRIORITIES = ("urgent", "high", "normal", "low")
SPACES = ("work", "personal")
EDITABLE = {"title", "notes", "status", "priority", "due_date", "due_time", "planned_date",
            "project", "requester", "source_url", "waiting_on", "sort_order", "space"}


_LOCAL_TZ: ZoneInfo | None = None


def _system_tz() -> ZoneInfo:
    """The machine's zone: /etc/localtime points into the zoneinfo database on macOS and Linux."""
    try:
        target = os.path.realpath("/etc/localtime")
        return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (IndexError, KeyError, ValueError, OSError):
        return ZoneInfo("UTC")


def tz() -> ZoneInfo:
    global _LOCAL_TZ
    if os.environ.get("KARYA_TZ"):
        return ZoneInfo(os.environ["KARYA_TZ"])
    if _LOCAL_TZ is None:
        configured = load_config().get("timezone")
        _LOCAL_TZ = ZoneInfo(configured) if configured else _system_tz()
    return _LOCAL_TZ


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today(at: datetime | None = None) -> date:
    return (at or datetime.now(timezone.utc)).astimezone(tz()).date()


def local_date_of(stamp: str | None) -> str | None:
    """Local calendar date of a UTC timestamp."""
    if not stamp:
        return None
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz()).date().isoformat()


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_day(value: Any, *, base: date | None = None) -> str | None:
    """Turn a loose day expression into YYYY-MM-DD, or None.

    Accepts ISO dates and datetimes, today/tomorrow/yesterday, weekday names
    (the next occurrence, today included), "next week" (next Monday),
    "eow" (this Friday), "+3d" / "in 3 days", and "inbox"/"none" for None.
    Raises ValueError for anything it does not understand, so a typo never
    silently becomes "no date".
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "none", "null", "inbox", "someday", "backlog"):
        return None
    base = base or today()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    if re.match(r"\d{4}-\d{2}-\d{2}t", text):
        stamp = datetime.fromisoformat(text.upper().replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return stamp.date().isoformat()
        return stamp.astimezone(tz()).date().isoformat()
    if text in ("today", "tod", "eod", "now"):
        return base.isoformat()
    if text in ("tomorrow", "tmr", "tmrw"):
        return (base + timedelta(days=1)).isoformat()
    if text == "yesterday":
        return (base - timedelta(days=1)).isoformat()
    if text in ("next week", "nextweek", "next monday"):
        return (base + timedelta(days=7 - base.weekday())).isoformat()
    if text in ("eow", "end of week", "this week"):
        return (base + timedelta(days=(4 - base.weekday()) % 7)).isoformat()
    match = re.fullmatch(r"(?:\+|in )?(\d{1,3}) ?(d|day|days|w|week|weeks)", text)
    if match:
        count = int(match.group(1)) * (7 if match.group(2).startswith("w") else 1)
        return (base + timedelta(days=count)).isoformat()
    for index, name in enumerate(WEEKDAYS):
        if text in (name, name[:3], name[:4]):
            return (base + timedelta(days=(index - base.weekday()) % 7)).isoformat()
    raise ValueError(f"cannot read a date from {value!r}")


def resolve_clock(hour: int, minute: int = 0, ampm: str | None = None, *, now: datetime | None = None) -> str:
    """HH:MM for a clock time. With am/pm or a 24-hour hour it's exact; a bare
    1-12 ("before 7") means the next time that hour comes round: 7 said at
    15:00 is 19:00, said at 06:00 it's 07:00."""
    if not (0 <= minute < 60) or not (0 <= hour <= 23):
        raise ValueError(f"not a clock time: {hour}:{minute:02d}")
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"not a clock time: {hour}{ampm}")
        return f"{hour % 12 + (12 if ampm.lower() == 'pm' else 0):02d}:{minute:02d}"
    if hour == 0 or hour > 12:
        return f"{hour:02d}:{minute:02d}"
    now = (now or datetime.now(timezone.utc)).astimezone(tz())
    for candidate in (hour % 12, hour % 12 + 12):
        if (candidate, minute) > (now.hour, now.minute):
            return f"{candidate:02d}:{minute:02d}"
    return f"{hour % 12:02d}:{minute:02d}"  # both gone today: the morning one, tomorrow


def parse_time(value: Any) -> str | None:
    """'7pm', '7:30 pm', '19:00', '7' -> 'HH:MM' (24-hour), or None to clear."""
    if value is None:
        return None
    text = str(value).strip().lower().replace(".", "")
    if text in ("", "none", "null"):
        return None
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not match:
        raise ValueError(f"cannot read a time from {value!r}")
    return resolve_clock(int(match.group(1)), int(match.group(2) or 0), match.group(3))


def day_for_time(due_time: str, *, now: datetime | None = None) -> str:
    """The date a time-only deadline refers to: today if it's still ahead, else tomorrow."""
    now = (now or datetime.now(timezone.utc)).astimezone(tz())
    hh, mm = (int(x) for x in due_time.split(":"))
    return (now.date() if (hh, mm) > (now.hour, now.minute) else now.date() + timedelta(days=1)).isoformat()


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "to", "for", "of", "and", "on", "in", "with", "fix", "add", "check", "update"}
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in stop}


ISSUE_ID = re.compile(r"\b([A-Z][A-Z0-9]{1,5}-\d{1,6})\b")


def ref_key(source: str, ref: Any) -> str:
    """One spelling per source item, so the same Slack thread or Linear issue is
    always recognised: Slack refs reduce to the thread timestamp (the old app
    stored a bare ts, the sync stores channel:ts), Linear IDs are upper-cased."""
    text = str(ref or "").strip()
    source = (source or "").lower()
    if source == "slack":
        text = text.rsplit(":", 1)[-1]
        text = text.split("/p")[-1] if "/p" in text else text
    if source == "linear":
        text = text.upper()
    return text


# Words that say nothing about *which* task it is: verbs every title starts
# with and filler. Similarity is scored on what is left, so "Fix Acme login
# redirect" and "Fix Acme invoice rounding" don't look alike just because
# both fix something. The client or product names most of your tasks share
# belong here too: list them as ``similarity_ignore`` in config.json.
GENERIC = {"the", "a", "an", "to", "for", "of", "and", "or", "on", "in", "with", "by", "vs", "from", "into", "at",
           "fix", "add", "update", "check", "review", "apply", "make", "build", "verify", "resolve", "handle",
           "logic", "code", "coding", "codes", "rule", "rules", "issue", "task", "work", "bug", "feature", "support",
           "new", "change", "changes", "when", "not", "only", "all", "this", "that"}
_IGNORED: set[str] | None = None


def ignored_words() -> set[str]:
    global _IGNORED
    if _IGNORED is None:
        _IGNORED = GENERIC | {str(w).lower() for w in load_config().get("similarity_ignore") or []}
    return _IGNORED


def content_tokens(text: str) -> set[str]:
    words = set()
    ignored = ignored_words()
    for raw in re.findall(r"[a-z0-9][a-z0-9.]*", (text or "").lower()):
        raw = raw.strip(".")
        if len(raw) < 2 or raw in ignored:
            continue
        if len(raw) > 4 and raw.endswith("s") and not any(ch.isdigit() for ch in raw):
            raw = raw[:-1]  # charts -> chart
        words.add(raw)
    return words


def distinctive(tokens: set[str]) -> set[str]:
    """Tokens that pin down a task on their own: codes like 0503f, 59430, n95.1, z3a."""
    return {t for t in tokens if any(ch.isdigit() for ch in t) and len(t) >= 3}


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(path or os.environ.get("KARYA_DB") or ROOT / "data" / "karya.sqlite3")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        # Called as fn(store, before, after) after every committed change to a
        # task (after is None when it was deleted). The entry points register
        # alarms.on_change here; tests and scripts that don't, never touch Slack.
        self.listeners: list = []
        self._search = None  # search.SearchIndex, built on first use
        self._schema()

    # ------------------------------------------------------------------ schema
    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              notes TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'todo',
              priority TEXT NOT NULL DEFAULT 'normal',
              due_date TEXT,
              due_time TEXT,
              planned_date TEXT,
              project TEXT,
              source TEXT NOT NULL DEFAULT 'manual',
              source_ref TEXT,
              source_url TEXT,
              requester TEXT,
              waiting_on TEXT,
              sort_order REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT,
              completed_at TEXT,
              completion_note TEXT,
              space TEXT NOT NULL DEFAULT 'work'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_source ON tasks(source, source_ref) WHERE source_ref IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_tasks_open ON tasks(status, planned_date);
            CREATE INDEX IF NOT EXISTS ix_tasks_completed ON tasks(completed_at);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
              at TEXT NOT NULL,
              kind TEXT NOT NULL,
              actor TEXT NOT NULL DEFAULT 'karya',
              text TEXT NOT NULL DEFAULT '',
              data TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id, at);
            CREATE TABLE IF NOT EXISTS sync_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              status TEXT NOT NULL DEFAULT 'running',
              since TEXT,
              created INTEGER NOT NULL DEFAULT 0,
              updated INTEGER NOT NULL DEFAULT 0,
              closed INTEGER NOT NULL DEFAULT 0,
              message TEXT NOT NULL DEFAULT '',
              cost_usd REAL
            );
            CREATE TABLE IF NOT EXISTS notifications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              at TEXT NOT NULL,
              channel TEXT NOT NULL,
              ok INTEGER NOT NULL,
              fingerprint TEXT,
              text TEXT NOT NULL DEFAULT '',
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_refs (
              source TEXT NOT NULL,
              ref_key TEXT NOT NULL,
              task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
              PRIMARY KEY (source, ref_key)
            );
            CREATE TABLE IF NOT EXISTS alarms (
              task_id TEXT PRIMARY KEY,
              scheduled_id TEXT,
              channel TEXT,
              post_at INTEGER,
              due_at TEXT NOT NULL
            );
            """
        )
        # Every task's own source reference is also a row in task_refs, so one
        # lookup finds a task by any reference it carries. Backfill once; when two
        # tasks claim the same item, the open one (then the newest) wins.
        if not self.db.execute("SELECT 1 FROM task_refs LIMIT 1").fetchone():
            rows = self.db.execute(
                "SELECT id, source, source_ref FROM tasks WHERE source_ref IS NOT NULL "
                f"ORDER BY status IN ({','.join('?' * len(OPEN))}), updated_at", OPEN).fetchall()
            for row in rows:
                self.db.execute("INSERT OR REPLACE INTO task_refs(source, ref_key, task_id) VALUES (?,?,?)",
                                (row["source"], ref_key(row["source"], row["source_ref"]), row["id"]))
        # Stores created before Work/Personal existed: every old task was work.
        if "space" not in {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN space TEXT NOT NULL DEFAULT 'work'")
        self.db.commit()

    def close(self) -> None:
        if self._search is not None:
            self._search.close()
        self.db.close()

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        task = dict(row)
        task["open"] = task["status"] in OPEN
        task["done_date"] = local_date_of(task["completed_at"])
        return task

    def _event(self, task_id: str | None, kind: str, text: str = "", actor: str = "karya", data: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO events(task_id, at, kind, actor, text, data) VALUES (?,?,?,?,?,?)",
            (task_id, now_utc(), kind, actor, text or "", json.dumps(data or {}, ensure_ascii=False)),
        )

    def _notify(self, before: dict[str, Any] | None, after: dict[str, Any] | None) -> None:
        for listener in self.listeners:
            try:
                listener(self, before, after)
            except Exception as exc:  # noqa: BLE001 - a reminder failure must never undo the edit
                import sys
                print(f"karya listener {getattr(listener, '__name__', listener)} failed: {exc}", file=sys.stderr)

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self._row(self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def require(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if not task:
            raise KeyError(f"no task with id {task_id!r}")
        return task

    def by_source(self, source: str, source_ref: str) -> dict[str, Any] | None:
        """The task that tracks this Slack thread / Linear issue / email, by any reference it carries."""
        source = (source or "").lower()
        row = self.db.execute("SELECT task_id FROM task_refs WHERE source = ? AND ref_key = ?",
                              (source, ref_key(source, source_ref))).fetchone()
        if row:
            return self.get(row["task_id"])
        return self._row(self.db.execute(
            "SELECT * FROM tasks WHERE source = ? AND source_ref = ?", (source, str(source_ref))).fetchone())

    def add_ref(self, task_id: str, source: str, source_ref: str, *, actor: str = "sync", note: str | None = None) -> bool:
        """Attach another item (a Slack thread about a Linear issue, say) to a task.
        Returns False if that item already belongs to a task."""
        source = (source or "").lower()
        if source == "linear" and self.linear_ref(task_id) not in (None, ref_key(source, source_ref)):
            return False  # one Linear issue per task: a second issue is a second task
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO task_refs(source, ref_key, task_id) VALUES (?,?,?)",
                                  (source, ref_key(source, source_ref), task_id))
            if cur.rowcount:
                self._event(task_id, "linked", note or f"Also tracked in {source}: {source_ref}", actor,
                            {"source": source, "source_ref": source_ref})
            self.db.commit()
            return bool(cur.rowcount)

    def linear_ref(self, task_id: str) -> str | None:
        row = self.db.execute("SELECT ref_key FROM task_refs WHERE task_id = ? AND source = 'linear'", (task_id,)).fetchone()
        return row["ref_key"] if row else None

    def refs(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT source, ref_key FROM task_refs WHERE task_id = ?", (task_id,))]

    def by_mention(self, *texts: str) -> dict[str, Any] | None:
        """An open task whose Linear ID (ENG-2427, OPS-447...) is written in the text."""
        for text in texts:
            for ident in ISSUE_ID.findall(text or ""):
                task = self.by_source("linear", ident)
                if task and task["open"]:
                    return task
        return None

    def candidates(self, title: str, text: str = "", *, exclude: str | None = None, limit: int = 5,
                   floor: float = 0.2) -> list[dict[str, Any]]:
        """Open tasks that may be the same work, best first, each with a score
        and the words they share. Scored on content words (see GENERIC), with
        extra weight for distinctive codes both mention."""
        mine = content_tokens(f"{title} {text}")
        mine_title = content_tokens(title)
        if not mine:
            return []
        scored = []
        for task in self.list(limit=2000):
            if task["id"] == exclude:
                continue
            theirs = content_tokens(f"{task['title']} {task.get('notes') or ''}")
            theirs_title = content_tokens(task["title"])
            if not theirs:
                continue
            shared = mine & theirs
            if not shared:
                continue
            title_overlap = len(mine_title & theirs_title) / max(1, len(mine_title | theirs_title))
            body_overlap = len(shared) / max(1, min(len(mine), len(theirs)))
            codes = distinctive(shared)
            score = max(title_overlap, 0.6 * body_overlap) + 0.15 * min(len(codes), 3)
            score = round(min(score, 1.0), 2)
            if score >= floor:
                scored.append((score, task, sorted(shared)))
        scored.sort(key=lambda item: -item[0])
        return [dict(task, match=score, shared=shared) for score, task, shared in scored[:limit]]

    def by_ref(self, source_ref: str) -> dict[str, Any] | None:
        """Any task carrying this reference, open ones first - an agent told
        'work on ENG-2391' should find the Linear task, whatever its source."""
        return self._row(self.db.execute(
            f"SELECT * FROM tasks WHERE source_ref = ? ORDER BY status IN ({','.join('?' * len(OPEN))}) DESC, updated_at DESC LIMIT 1",
            (str(source_ref), *OPEN)).fetchone())

    def _clean(self, fields: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in EDITABLE:
                continue
            if key in ("due_date", "planned_date"):
                value = parse_day(value)
            elif key == "due_time":
                value = parse_time(value)
            elif key == "status":
                value = str(value).lower()
                if value not in STATUSES:
                    raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            elif key == "priority":
                value = {"medium": "normal", "none": "low", "p0": "urgent", "p1": "high"}.get(str(value).lower(), str(value).lower())
                if value not in PRIORITIES:
                    raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
            elif key == "space":
                value = str(value).lower()
                if value not in SPACES:
                    raise ValueError(f"space must be one of {', '.join(SPACES)}")
            elif key == "title":
                value = " ".join(str(value).split())
                if not value:
                    raise ValueError("title cannot be empty")
            elif key == "sort_order":
                value = float(value)
            elif isinstance(value, str) and key != "notes":
                value = value.strip() or None
            clean[key] = value
        return clean

    @staticmethod
    def _default_plan(due_date: str | None, base: date) -> str | None:
        """A deadline lands the task on its due day; a past deadline lands it on today."""
        if not due_date:
            return None
        return max(due_date, base.isoformat())

    # ---------------------------------------------------------------- mutation
    def create(self, title: str, *, actor: str = "manual", source: str = "manual", source_ref: str | None = None,
               note: str | None = None, plan: bool = True, **fields: Any) -> dict[str, Any]:
        with self.lock:
            clean = self._clean({"title": title, **fields})
            base = today()
            status = clean.get("status", "todo")
            if clean.get("due_time") and not clean.get("due_date"):
                clean["due_date"] = day_for_time(clean["due_time"])
            if "planned_date" not in clean and plan:
                # Work in progress and urgent work without a deadline belong on today;
                # anything else with a deadline lands on its due day, the rest in the Inbox.
                if status == "doing" or (clean.get("priority") == "urgent" and not clean.get("due_date")):
                    clean["planned_date"] = base.isoformat()
                else:
                    clean["planned_date"] = self._default_plan(clean.get("due_date"), base)
            stamp = now_utc()
            task_id = uuid.uuid4().hex[:10]
            row = {
                "id": task_id, "title": clean["title"], "notes": clean.get("notes") or "", "status": status,
                "priority": clean.get("priority", "normal"), "due_date": clean.get("due_date"),
                "due_time": clean.get("due_time"), "planned_date": clean.get("planned_date"),
                "project": clean.get("project"), "source": (source or "manual").lower(),
                "source_ref": str(source_ref) if source_ref else None, "source_url": clean.get("source_url"),
                "requester": clean.get("requester"), "waiting_on": clean.get("waiting_on"),
                "space": clean.get("space", "work"),
                "sort_order": clean.get("sort_order", datetime.now(timezone.utc).timestamp()),
                "created_at": stamp, "updated_at": stamp,
                "started_at": stamp if status == "doing" else None,
                "completed_at": stamp if status == "done" else None,
                "completion_note": None,
            }
            self.db.execute(f"INSERT INTO tasks({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
            if row["source_ref"]:
                self.db.execute("INSERT OR IGNORE INTO task_refs(source, ref_key, task_id) VALUES (?,?,?)",
                                (row["source"], ref_key(row["source"], row["source_ref"]), task_id))
            self._event(task_id, "created", note or f"Added from {row['source']}", actor,
                        {"source": row["source"], "source_ref": row["source_ref"]})
            if status == "doing":
                self._event(task_id, "started", "Work started", actor)
            self.db.commit()
            task = self.get(task_id)
        self._notify(None, task)
        return task  # type: ignore[return-value]

    def update(self, task_id: str, patch: dict[str, Any], *, actor: str = "manual", note: str | None = None,
               kind: str | None = None) -> dict[str, Any]:
        with self.lock:
            current = self.require(task_id)
            clean = self._clean(patch)
            changes = {k: v for k, v in clean.items() if current.get(k) != v}
            if changes.get("due_time") and not (changes.get("due_date") or current["due_date"]):
                changes["due_date"] = day_for_time(changes["due_time"])
                if current["planned_date"] is None:
                    changes.setdefault("planned_date", self._default_plan(changes["due_date"], today()))
            if not changes and not note:
                return current
            stamp = now_utc()
            status = changes.get("status")
            if status == "doing" and not current["started_at"]:
                changes["started_at"] = stamp
            if status == "done":
                changes["completed_at"] = stamp
            elif status in OPEN and current["status"] in ("done", "dropped"):
                changes["completed_at"] = None
                changes.setdefault("planned_date", today().isoformat())
            if status == "doing" and "planned_date" not in changes and current["planned_date"] != today().isoformat():
                changes["planned_date"] = today().isoformat()
            if changes:
                sets = ", ".join(f"{k} = ?" for k in changes)
                self.db.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), stamp, task_id))
            else:
                self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, task_id))
            if kind is None:
                if status == "done":
                    kind = "completed"
                elif status == "dropped":
                    kind = "dropped"
                elif status == "doing" and current["status"] != "doing":
                    kind = "started"
                elif status in OPEN and not current["open"]:
                    kind = "reopened"
                elif set(changes) <= {"planned_date", "sort_order"} and "planned_date" in changes:
                    kind = "rescheduled"
                elif changes:
                    kind = "updated"
                else:
                    kind = "note"
            visible = {k: v for k, v in changes.items() if k not in ("updated_at", "sort_order", "started_at", "completed_at")}
            self._event(task_id, kind, note or self._describe(visible), actor, {"changes": visible})
            self.db.commit()
            task = self.get(task_id)
        if changes:
            self._notify(current, task)
        return task  # type: ignore[return-value]

    @staticmethod
    def _describe(changes: dict[str, Any]) -> str:
        if not changes:
            return ""
        parts = []
        for key, value in changes.items():
            label = key.replace("_", " ")
            parts.append(f"{label} → {value}" if value not in (None, "") else f"{label} cleared")
        return "; ".join(parts)

    def start(self, task_id: str, *, actor: str = "claude", note: str | None = None) -> dict[str, Any]:
        return self.update(task_id, {"status": "doing"}, actor=actor, note=note or "Work started")

    def complete(self, task_id: str, summary: str | None = None, *, actor: str = "manual") -> dict[str, Any]:
        with self.lock:
            task = self.update(task_id, {"status": "done"}, actor=actor, note=summary or "Marked done")
            if summary:
                self.db.execute("UPDATE tasks SET completion_note = ? WHERE id = ?", (summary.strip(), task_id))
                self.db.commit()
            return self.get(task_id)  # type: ignore[return-value]

    def reopen(self, task_id: str, *, actor: str = "manual") -> dict[str, Any]:
        return self.update(task_id, {"status": "todo"}, actor=actor, note="Reopened")

    def log(self, task_id: str, text: str, *, actor: str = "manual", kind: str = "progress",
            patch: dict[str, Any] | None = None) -> dict[str, Any]:
        if patch:
            return self.update(task_id, patch, actor=actor, note=text, kind=kind)
        with self.lock:
            self.require(task_id)
            self.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now_utc(), task_id))
            self._event(task_id, kind, text, actor)
            self.db.commit()
            return self.get(task_id)  # type: ignore[return-value]

    def merge(self, keep_id: str, drop_id: str, *, actor: str = "manual") -> dict[str, Any]:
        """Fold a duplicate into the task to keep. The kept task takes the
        duplicate's references, history, notes, link and deadline (earliest
        wins), and its progress if it is further along. The duplicate is
        dropped, not deleted, with a pointer to where it went."""
        if keep_id == drop_id:
            raise ValueError("cannot merge a task into itself")
        with self.lock:
            keep, drop = self.require(keep_id), self.require(drop_id)
            if self.linear_ref(keep_id) and self.linear_ref(drop_id) and self.linear_ref(keep_id) != self.linear_ref(drop_id):
                raise ValueError(f"{keep_id} and {drop_id} track different Linear issues; they are separate tasks")
            self.db.execute("UPDATE OR IGNORE task_refs SET task_id = ? WHERE task_id = ?", (keep_id, drop_id))
            self.db.execute("DELETE FROM task_refs WHERE task_id = ?", (drop_id,))
            self.db.execute("UPDATE events SET task_id = ? WHERE task_id = ?", (keep_id, drop_id))
            self.db.commit()
        rank = {"todo": 0, "doing": 1, "waiting": 2}
        patch: dict[str, Any] = {}
        notes = "\n\n".join(n for n in (keep["notes"], drop["notes"]) if n and n.strip())
        if notes != keep["notes"]:
            patch["notes"] = notes
        if drop["open"] and rank.get(drop["status"], 0) > rank.get(keep["status"], 0) and keep["open"]:
            patch["status"] = drop["status"]
            if drop.get("waiting_on"):
                patch["waiting_on"] = drop["waiting_on"]
        if not keep.get("source_url") and drop.get("source_url"):
            patch["source_url"] = drop["source_url"]
        if drop.get("due_date") and (not keep.get("due_date") or drop["due_date"] < keep["due_date"]):
            patch["due_date"] = drop["due_date"]
            if drop.get("due_time"):
                patch["due_time"] = drop["due_time"]
        for key in ("project", "requester"):
            if not keep.get(key) and drop.get(key):
                patch[key] = drop[key]
        self.update(keep_id, patch, actor=actor, note=f"Merged in duplicate \"{drop['title']}\"", kind="merged")
        if drop["open"]:
            self.update(drop_id, {"status": "dropped"}, actor=actor, note=f"Duplicate of {keep_id} (\"{keep['title']}\")")
        return self.get(keep_id)  # type: ignore[return-value]

    def delete(self, task_id: str) -> None:
        with self.lock:
            before = self.get(task_id)
            self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self.db.commit()
        if before:
            self._notify(before, None)

    # ------------------------------------------------------------ alarm rows
    def alarm_get(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM alarms WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def alarm_set(self, task_id: str, scheduled_id: str | None, channel: str, post_at: int, due_at: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO alarms(task_id, scheduled_id, channel, post_at, due_at) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(task_id) DO UPDATE SET scheduled_id=excluded.scheduled_id, channel=excluded.channel, "
                            "post_at=excluded.post_at, due_at=excluded.due_at", (task_id, scheduled_id, channel, post_at, due_at))
            self.db.commit()

    def alarm_drop(self, task_id: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM alarms WHERE task_id = ?", (task_id,))
            self.db.commit()

    def alarms(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM alarms ORDER BY post_at")]

    # ------------------------------------------------------- source ingestion
    def ingest(self, item: dict[str, Any], *, actor: str = "sync") -> tuple[dict[str, Any], str]:
        """Create or refresh a task from Slack/Gmail/Linear/Calendar.

        Returns (task, outcome) where outcome is created | updated | unchanged
        | skipped. A task you already finished or dropped is never reopened
        by a sync; your decision wins over the source.
        """
        source = str(item.get("source") or "").lower().strip()
        ref = str(item.get("source_ref") or "").strip()
        title = " ".join(str(item.get("title") or "").split())
        if not source or not ref or not title:
            raise ValueError("ingest needs source, source_ref and title")
        fields = {k: item.get(k) for k in ("notes", "priority", "due_date", "due_time", "project", "requester", "source_url")
                  if item.get(k) not in (None, "")}
        with self.lock:
            existing = self.by_source(source, ref)
            if not existing:
                task = self.create(title, actor=actor, source=source, source_ref=ref,
                                   note=item.get("reason") or f"Picked up from {source}", **fields)
                return task, "created"
            if not existing["open"]:
                return existing, "skipped"
            # Refresh only what the source owns: its deadline, link and requester.
            # Title, priority and plan are yours once the task exists.
            patch = {k: fields[k] for k in ("due_date", "due_time", "source_url", "requester") if k in fields}
            if patch.get("due_date") and existing["due_date"] != parse_day(patch["due_date"]):
                new_due = parse_day(patch["due_date"])
                if existing["planned_date"] is None or (existing["due_date"] and existing["planned_date"] == max(existing["due_date"], today().isoformat())):
                    patch["planned_date"] = self._default_plan(new_due, today())
            clean = self._clean(patch)
            if all(existing.get(k) == v for k, v in clean.items()):
                return existing, "unchanged"
            return self.update(existing["id"], clean, actor=actor, note=item.get("reason") or f"Refreshed from {source}"), "updated"

    def import_record(self, title: str, *, source: str, source_ref: str, status: str, created_at: str,
                      completed_at: str | None = None, completion_note: str | None = None,
                      history: Iterable[tuple[str, str, str]] = (), actor: str = "import",
                      **fields: Any) -> tuple[dict[str, Any], bool]:
        """Record work that happened before Kārya saw it (a merged PR, a commit)
        with its real timestamps, so it lands on the day it was done and its
        history reads as it happened. history is (at, kind, text) events.
        Idempotent by (source, source_ref): returns (task, created).

        Only the item's own reference is stored. Linear IDs stay in the text
        (searchable) and are never claimed as references: a finished import
        owning ENG-2430 would make the sync skip that issue's live task."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        with self.lock:
            existing = self.by_source(source, source_ref)
            if existing:
                return existing, False
            clean = self._clean({"title": title, **fields})
            task_id = uuid.uuid4().hex[:10]
            row = {
                "id": task_id, "title": clean["title"], "notes": clean.get("notes") or "", "status": status,
                "priority": clean.get("priority", "normal"), "due_date": None, "due_time": None,
                "planned_date": clean.get("planned_date"), "project": clean.get("project"),
                "source": source.lower(), "source_ref": source_ref, "source_url": clean.get("source_url"),
                "requester": clean.get("requester"), "waiting_on": clean.get("waiting_on"),
                "space": clean.get("space", "work"),
                "sort_order": datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp(),
                "created_at": created_at, "updated_at": completed_at or created_at, "started_at": created_at,
                "completed_at": completed_at if status == "done" else None,
                "completion_note": completion_note,
            }
            self.db.execute(f"INSERT INTO tasks({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
            self.db.execute("INSERT OR IGNORE INTO task_refs(source, ref_key, task_id) VALUES (?,?,?)",
                            (row["source"], ref_key(row["source"], source_ref), task_id))
            for at, kind, text in history:
                self.db.execute("INSERT INTO events(task_id, at, kind, actor, text, data) VALUES (?,?,?,?,?,?)",
                                (task_id, at, kind, actor, text, json.dumps({"imported": True})))
            self.db.commit()
            return self.get(task_id), True  # type: ignore[return-value]

    def close_from_source(self, source: str, source_ref: str, summary: str, *, actor: str = "sync") -> dict[str, Any] | None:
        task = self.by_source(source.lower(), str(source_ref))
        if not task or not task["open"]:
            return None
        return self.complete(task["id"], summary, actor=actor)

    # ----------------------------------------------------------------- planner
    def rollover(self) -> int:
        """Ellie-style rollover: anything planned for a past day that is still
        open moves to today. Done tasks stay where they were finished."""
        base = today().isoformat()
        with self.lock:
            rows = self.db.execute(
                f"SELECT id, planned_date FROM tasks WHERE status IN ({','.join('?' * len(OPEN))}) AND planned_date < ?",
                (*OPEN, base)).fetchall()
            for row in rows:
                self.db.execute("UPDATE tasks SET planned_date = ?, updated_at = ? WHERE id = ?", (base, now_utc(), row["id"]))
                self._event(row["id"], "rolled_over", f"Moved from {row['planned_date']} to today", "karya",
                            {"from": row["planned_date"], "to": base})
            if rows:
                self.db.commit()
            return len(rows)

    # ------------------------------------------------------------------ reads
    def board(self, start: str, end: str) -> dict[str, Any]:
        """Everything the plan view needs for [start, end]: open tasks planned
        in the window, the Inbox, and tasks completed in the window."""
        self.rollover()
        open_rows = self.db.execute(
            f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY sort_order", OPEN).fetchall()
        # completed_at is UTC; widen by a day either side, then filter on the local date.
        lo = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        hi = (date.fromisoformat(end) + timedelta(days=2)).isoformat()
        done_rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ? ORDER BY completed_at",
            (lo, hi)).fetchall()
        tasks = [self._row(r) for r in open_rows]
        done = [t for t in (self._row(r) for r in done_rows) if t and start <= (t["done_date"] or "") <= end]
        return {"tasks": tasks, "done": done}

    def list(self, *, statuses: Iterable[str] = OPEN, limit: int = 200) -> list[dict[str, Any]]:
        statuses = tuple(statuses)
        rows = self.db.execute(
            f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(statuses))}) "
            "ORDER BY COALESCE(planned_date, '9999'), CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, sort_order LIMIT ?",
            (*statuses, limit)).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def done_between(self, start: str, end: str, *, space: str | None = None) -> list[dict[str, Any]]:
        lo = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        hi = (date.fromisoformat(end) + timedelta(days=2)).isoformat()
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'done' AND completed_at >= ? AND completed_at < ? ORDER BY completed_at DESC",
            (lo, hi)).fetchall()
        return [t for t in (self._row(r) for r in rows)
                if t and start <= (t["done_date"] or "") <= end and (space is None or t["space"] == space)]

    def search(self, query: str, *, status: str = "all", limit: int = 30) -> dict[str, Any]:
        """Ranked search over titles, outcomes, notes, every log entry and the
        references (see search.py). status: all | open | done."""
        with self.lock:
            if self._search is None:
                from search import SearchIndex
                self._search = SearchIndex(self)
            return self._search.search(query, status=status, limit=limit)

    def related_open(self, title: str, *, exclude: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
        """Open tasks whose titles share most of their words with ``title`` -
        so an agent starting 'fix the checkout feedback' finds the Slack task
        'Fix Acme checkout feedback before the next release' instead of cloning it."""
        mine = _tokens(title)
        if not mine:
            return []
        scored = []
        for task in self.list(limit=500):
            if task["id"] == exclude:
                continue
            theirs = _tokens(task["title"])
            if not theirs:
                continue
            overlap = len(mine & theirs) / min(len(mine), len(theirs))
            if overlap >= 0.5:
                scored.append((overlap, task))
        scored.sort(key=lambda pair: -pair[0])
        return [dict(task, match=round(score, 2)) for score, task in scored[:limit]]

    def find_open_by_title(self, title: str) -> dict[str, Any] | None:
        return self._row(self.db.execute(
            f"SELECT * FROM tasks WHERE lower(title) = lower(?) AND status IN ({','.join('?' * len(OPEN))}) ORDER BY updated_at DESC LIMIT 1",
            (" ".join(title.split()), *OPEN)).fetchone())

    def events(self, task_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if task_id:
            rows = self.db.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT e.*, t.title AS task_title FROM events e LEFT JOIN tasks t ON t.id = e.task_id ORDER BY e.id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def digest(self, on: date | None = None) -> dict[str, Any]:
        """What needs attention, for reminders and for the header counts.

        ``on`` previews a future day without changing anything: the 21:00 run
        uses it to write tomorrow morning's list, counting today's unfinished
        work as rolled over and today's deadlines as overdue, exactly as they
        will be when the day starts."""
        self.rollover()
        base = on or today()
        t0, t1, t2 = base.isoformat(), (base + timedelta(days=1)).isoformat(), (base + timedelta(days=2)).isoformat()
        tasks = self.list(limit=1000)
        if on:
            tasks = [dict(t, planned_date=max(t["planned_date"], t0)) if t["planned_date"] else t for t in tasks]
        overdue = [t for t in tasks if t["due_date"] and t["due_date"] < t0]
        urgent = [t for t in tasks if t["priority"] == "urgent"]
        due_today = [t for t in tasks if t["due_date"] == t0]
        due_soon = [t for t in tasks if t["due_date"] in (t1, t2)]
        planned_today = [t for t in tasks if t["planned_date"] == t0]
        waiting = [t for t in tasks if t["status"] == "waiting"]
        doing = [t for t in tasks if t["status"] == "doing"]
        inbox = [t for t in tasks if t["planned_date"] is None]
        done_today = self.done_between(t0, t0)
        return {
            "today": t0, "overdue": overdue, "due_today": due_today, "due_soon": due_soon, "urgent": urgent,
            "planned_today": planned_today, "waiting": waiting, "doing": doing, "inbox": inbox,
            "done_today": done_today,
            "counts": {"open": len(tasks), "overdue": len(overdue), "due_today": len(due_today),
                       "due_soon": len(due_soon), "urgent": len(urgent), "planned_today": len(planned_today),
                       "inbox": len(inbox), "waiting": len(waiting), "done_today": len(done_today)},
        }

    def review(self, start: str, end: str, *, space: str | None = "work") -> dict[str, Any]:
        """Completed work in a period, grouped by project, as data and Markdown,
        with the outcome noted at the time. Work only unless asked otherwise:
        personal tasks never appear in the work record."""
        done = self.done_between(start, end, space=space)
        groups: dict[str, list[dict[str, Any]]] = {}
        for task in sorted(done, key=lambda t: t["completed_at"] or ""):
            groups.setdefault(task["project"] or "Other", []).append(task)
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        lines = [f"# Work completed {start} to {end}", "", f"{len(done)} tasks completed.", ""]
        for project, items in ordered:
            lines.append(f"## {project} ({len(items)})")
            lines.append("")
            for task in items:
                when = datetime.fromisoformat(task["done_date"]).strftime("%d %b") if task["done_date"] else ""
                ref = f" [{task['source_ref']}]" if task["source"] == "linear" and task["source_ref"] else ""
                lines.append(f"- **{task['title']}**{ref} ({when})")
                if task.get("completion_note"):
                    lines.append(f"  {task['completion_note']}")
            lines.append("")
        return {"start": start, "end": end, "count": len(done),
                "groups": [{"project": p, "tasks": items} for p, items in ordered],
                "markdown": "\n".join(lines).rstrip() + "\n"}

    # ------------------------------------------------------------- sync + kv
    def begin_sync(self, since: str | None) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO sync_runs(started_at, since) VALUES (?, ?)", (now_utc(), since))
            self.db.commit()
            return int(cur.lastrowid)

    def finish_sync(self, run_id: int, *, status: str, created: int = 0, updated: int = 0, closed: int = 0,
                    message: str = "", cost_usd: float | None = None) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, created=?, updated=?, closed=?, message=?, cost_usd=? WHERE id=?",
                (now_utc(), status, created, updated, closed, message[:4000], cost_usd, run_id))
            self.db.commit()

    def sync_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def last_good_sync(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM sync_runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def record_notification(self, channel: str, ok: bool, text: str, fingerprint: str | None, error: str | None = None) -> None:
        with self.lock:
            self.db.execute("INSERT INTO notifications(at, channel, ok, fingerprint, text, error) VALUES (?,?,?,?,?,?)",
                            (now_utc(), channel, int(ok), fingerprint, text[:8000], error))
            self.db.commit()

    def last_notification(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM notifications WHERE ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.lock:
            self.db.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (key, json.dumps(value)))
            self.db.commit()


def load_config() -> dict[str, Any]:
    """config.json sits next to the code and holds who you are and how
    reminders reach you (install.sh writes it). It is gitignored and readable
    only by you, because it can hold a Slack bot token."""
    path = Path(os.environ.get("KARYA_CONFIG", ROOT / "config.json"))
    defaults: dict[str, Any] = {
        "name": "",
        "email": "",
        "slack_user_id": "",
        "slack_channel_id": "",
        "slack_webhook_url": "",
        "linear_user": "me",
        "port": 8788,
        "sync_model": "sonnet",
        "notify_model": "haiku",
        "claude_bin": str(Path.home() / ".local/bin/claude"),
        "macos_notifications": True,
    }
    if path.exists():
        defaults.update(json.loads(path.read_text()))
    return defaults
