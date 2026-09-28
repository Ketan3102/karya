#!/usr/bin/env python3
"""Kārya web app: a JSON API over the shared store plus the static board.
Loopback only and stdlib only, so launchd can keep it alive with no venv."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import alarms
from store import ROOT, Store, load_config, parse_day, resolve_clock, today

WEB = ROOT / "web"
STORE: Store
LOCK = threading.RLock()
SYNC_LOCK = ROOT / "data" / "sync.lock"


def sync_running() -> bool:
    try:
        pid = int(SYNC_LOCK.read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


# "before 7pm", "by 19:00", "at 6:30 pm", "7pm", "before 7": a clock time for the task.
TIME = re.compile(r"(?:\b(?:by|before|at|till|until)\s+|@)?\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b"
                  r"|(?:\b(?:by|before|at|till|until)\s+|@)\b(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:am|pm))"
                  r"|\b(\d{1,2}):(\d{2})\b", re.I)


def pull_time(text: str) -> tuple[str, str | None]:
    """Take the first clock time out of free text: ('Order dinner', '19:00')."""
    match = TIME.search(text)
    if not match:
        return text, None
    g = match.groups()
    try:
        if g[0]:
            when = resolve_clock(int(g[0]), int(g[1] or 0), g[2])
        elif g[3]:
            when = resolve_clock(int(g[3]), int(g[4] or 0))
        else:
            when = resolve_clock(int(g[5]), int(g[6]))
    except ValueError:
        return text, None
    return " ".join((text[:match.start()] + " " + text[match.end():]).split()), when


def parse_quick(text: str) -> dict:
    """'Send deck to Sam !high #Acme due:fri' -> title + priority/project/due.
    '@personal' (or @p) files it under Personal; tasks are Work by default.
    Only tokens that parse are consumed, so ordinary words are never eaten."""
    fields: dict = {}
    words = []
    for word in text.split():
        low = word.lower()
        if low in ("!!", "!urgent"):
            fields["priority"] = "urgent"
        elif low in ("!", "!high"):
            fields["priority"] = "high"
        elif low == "!low":
            fields["priority"] = "low"
        elif low in ("@personal", "@p", "@me"):
            fields["space"] = "personal"
        elif low in ("@work", "@w"):
            fields["space"] = "work"
        elif re.fullmatch(r"#[\w.-]+", word):
            fields["project"] = word[1:].replace("_", " ")
        elif low.startswith("due:"):
            try:
                fields["due_date"] = parse_day(word[4:].replace("_", " "))
            except ValueError:
                words.append(word)
        else:
            words.append(word)
    title, when = pull_time(" ".join(words))
    if when:
        fields["due_time"] = when
    fields["title"] = title
    return fields


class Handler(BaseHTTPRequestHandler):
    server_version = "Karya/2"  # HTTP headers are Latin-1: no macron

    def log_message(self, fmt, *args):  # quiet: launchd log stays readable
        if os.environ.get("KARYA_VERBOSE"):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- plumbing
    def _send(self, status: int, body, ctype: str = "application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def _route(self, method: str):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        if not path.startswith("/api/"):
            return self._static(path) if method == "GET" else self._send(404, {"error": "not found"})
        try:
            body = self._json() if method in ("POST", "PATCH") else {}
            with LOCK:
                result = self._api(method, path, query, body)
            if result is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, result)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc).strip("'\"")})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": str(exc)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    # --------------------------------------------------------------------- api
    def _api(self, method: str, path: str, q: dict, body: dict):
        s = STORE
        parts = path.split("/")[2:]  # after /api/
        if method == "GET" and parts == ["board"]:
            base = today()
            start = q.get("start") or (base - timedelta(days=7)).isoformat()
            end = q.get("end") or (base + timedelta(days=13)).isoformat()
            board = s.board(start, end)
            digest = s.digest()
            return {"today": base.isoformat(), "start": start, "end": end, **board,
                    "counts": digest["counts"], "sync": s.sync_runs(5), "sync_running": sync_running(),
                    "notified": s.last_notification()}
        if method == "GET" and parts == ["digest"]:
            return s.digest()
        if method == "GET" and parts == ["activity"]:
            return {"events": s.events(limit=int(q.get("limit", 60)))}
        if method == "GET" and parts == ["search"]:
            return s.search(q.get("q", ""), status=q.get("status") or "all", limit=min(int(q.get("limit", 40)), 100))
        if method == "GET" and parts == ["done"]:
            end = q.get("end") or today().isoformat()
            start = q.get("start") or (date.fromisoformat(end) - timedelta(days=30)).isoformat()
            space = q.get("space") if q.get("space") in ("work", "personal") else None
            return {"start": start, "end": end, "tasks": s.done_between(start, end, space=space)}
        if method == "GET" and parts == ["review"]:
            end = q.get("end") or today().isoformat()
            start = q.get("start") or (date.fromisoformat(end) - timedelta(days=90)).isoformat()
            return s.review(start, end, space="work")  # the work record never includes personal tasks
        if method == "POST" and parts == ["tasks"]:
            fields = parse_quick(body.pop("quick")) if body.get("quick") else {}
            fields.update({k: v for k, v in body.items() if v not in (None, "")})
            title = fields.pop("title", "")
            if "planned_date" in body and body["planned_date"] in (None, ""):
                fields["planned_date"] = None
            return {"task": s.create(title, actor="manual", note="Added on the board", **fields)}
        if method == "POST" and parts == ["sync"]:
            if sync_running():
                return {"started": False, "running": True}
            subprocess.Popen([sys.executable, str(ROOT / "sync.py")], cwd=ROOT, start_new_session=True,
                             stdout=open(ROOT / "data" / "sync.log", "a"), stderr=subprocess.STDOUT)
            return {"started": True, "running": True}
        if len(parts) >= 2 and parts[0] == "tasks":
            task_id = parts[1]
            if method == "GET" and len(parts) == 2:
                return {"task": s.require(task_id), "events": s.events(task_id)}
            if method == "PATCH" and len(parts) == 2:
                note = body.pop("note", None)
                if "planned_date" in body and body["planned_date"] in ("", None):
                    body["planned_date"] = None
                return {"task": s.update(task_id, body, actor="manual", note=note)}
            if method == "POST" and parts[2:] == ["complete"]:
                return {"task": s.complete(task_id, body.get("summary"), actor="manual")}
            if method == "POST" and parts[2:] == ["reopen"]:
                return {"task": s.reopen(task_id)}
            if method == "POST" and parts[2:] == ["log"]:
                return {"task": s.log(task_id, body.get("text", "").strip() or "Note", actor="manual", kind="note")}
        return None

    def _static(self, path: str):
        target = (WEB / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if WEB not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json", "svg+xml")):
            ctype += "; charset=utf-8"
        return self._send(200, target.read_bytes(), ctype)


def main() -> None:
    global STORE
    config = load_config()
    parser = argparse.ArgumentParser(description="Kārya board")
    parser.add_argument("--port", type=int, default=int(os.environ.get("KARYA_PORT", config["port"])))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    STORE = Store()
    STORE.listeners.append(alarms.on_change)  # a task with a clock time gets its Slack reminder
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Kārya on http://{args.host}:{args.port}  (db {STORE.path})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
