"""Run with:  python3 -m unittest discover -s tests   (from ~/karya)"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Hermetic: never read the real config.json. The date tests assume a UTC+05:30 zone.
_CONFIG_DIR = tempfile.mkdtemp(prefix="karya-test-")
os.environ["KARYA_CONFIG"] = os.path.join(_CONFIG_DIR, "config.json")
os.environ["KARYA_TZ"] = "Asia/Kolkata"
Path(os.environ["KARYA_CONFIG"]).write_text(json.dumps({
    "name": "Sam", "email": "sam@example.com", "slack_channel_id": "C0TESTCHAN",
    "search_synonyms": [["auth", "authentication"]], "similarity_ignore": ["acme", "billing"]}))

import store as store_mod  # noqa: E402
from store import Store, parse_day  # noqa: E402


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "t.sqlite3")
        self.s = Store(self.db)

    def tearDown(self):
        self.s.close()
        self.dir.cleanup()


class ParseDay(unittest.TestCase):
    base = date(2026, 9, 23)  # a Wednesday

    def test_words(self):
        p = lambda v: parse_day(v, base=self.base)  # noqa: E731
        self.assertEqual(p("today"), "2026-09-23")
        self.assertEqual(p("tomorrow"), "2026-09-24")
        self.assertEqual(p("fri"), "2026-09-25")
        self.assertEqual(p("wednesday"), "2026-09-23")  # today counts
        self.assertEqual(p("mon"), "2026-09-28")
        self.assertEqual(p("next week"), "2026-09-28")
        self.assertEqual(p("eow"), "2026-09-25")
        self.assertEqual(p("+3d"), "2026-09-26")
        self.assertEqual(p("in 2 weeks"), "2026-10-07")
        self.assertIsNone(p("inbox"))
        self.assertIsNone(p(""))

    def test_iso_datetime_is_read_in_local_time(self):
        # 20:00 UTC on the 23rd is 01:30 IST on the 24th.
        self.assertEqual(parse_day("2026-09-23T20:00:00Z"), "2026-09-24")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_day("sometime soon")


class Planning(Temp):
    def test_due_date_plans_on_due_day_and_past_due_plans_today(self):
        t0 = store_mod.today().isoformat()
        future = (store_mod.today() + timedelta(days=3)).isoformat()
        past = (store_mod.today() - timedelta(days=2)).isoformat()
        self.assertEqual(self.s.create("a", due_date=future)["planned_date"], future)
        self.assertEqual(self.s.create("b", due_date=past)["planned_date"], t0)
        self.assertIsNone(self.s.create("c")["planned_date"])
        self.assertEqual(self.s.create("d", status="doing")["planned_date"], t0)
        self.assertEqual(self.s.create("e", priority="urgent")["planned_date"], t0)
        self.assertEqual(self.s.create("f", priority="urgent", due_date=future)["planned_date"], future)

    def test_rollover_moves_unfinished_work_to_today_only(self):
        yesterday = (store_mod.today() - timedelta(days=1)).isoformat()
        open_task = self.s.create("open")
        done_task = self.s.create("done")
        self.s.db.execute("UPDATE tasks SET planned_date = ?", (yesterday,))
        self.s.db.commit()
        self.s.complete(done_task["id"], "shipped")
        self.s.db.execute("UPDATE tasks SET planned_date = ? WHERE id = ?", (yesterday, done_task["id"]))
        self.s.db.commit()
        self.assertEqual(self.s.rollover(), 1)
        self.assertEqual(self.s.get(open_task["id"])["planned_date"], store_mod.today().isoformat())
        self.assertEqual(self.s.get(done_task["id"])["planned_date"], yesterday)
        kinds = [e["kind"] for e in self.s.events(open_task["id"])]
        self.assertIn("rolled_over", kinds)

    def test_done_date_is_local(self):
        task = self.s.create("late night")
        self.s.complete(task["id"])
        # Completed at 19:00 UTC = 00:30 IST the next day.
        self.s.db.execute("UPDATE tasks SET completed_at = '2026-09-23T19:00:00Z' WHERE id = ?", (task["id"],))
        self.s.db.commit()
        self.assertEqual(self.s.get(task["id"])["done_date"], "2026-09-24")
        self.assertEqual([t["id"] for t in self.s.done_between("2026-09-24", "2026-09-24")], [task["id"]])
        self.assertEqual(self.s.done_between("2026-09-23", "2026-09-23"), [])

    def test_digest_counts_due_today_as_due_today_not_overdue(self):
        self.s.create("x", due_date="today")
        d = self.s.digest()
        self.assertEqual(d["counts"]["due_today"], 1)
        self.assertEqual(d["counts"]["overdue"], 0)

    def test_reopen_returns_to_today(self):
        task = self.s.create("x")
        self.s.complete(task["id"], "did it")
        again = self.s.reopen(task["id"])
        self.assertEqual(again["status"], "todo")
        self.assertIsNone(again["completed_at"])
        self.assertEqual(again["planned_date"], store_mod.today().isoformat())


class Ingest(Temp):
    item = {"source": "linear", "source_ref": "ENG-1", "title": "Fix thing", "due_date": "2026-12-01", "priority": "high"}

    def test_same_ref_never_duplicates(self):
        _, first = self.s.ingest(dict(self.item))
        _, second = self.s.ingest(dict(self.item))
        self.assertEqual((first, second), ("created", "unchanged"))
        self.assertEqual(len(self.s.list()), 1)

    def test_new_deadline_is_picked_up_and_moves_the_plan(self):
        task, _ = self.s.ingest(dict(self.item))
        self.assertEqual(task["planned_date"], "2026-12-01")
        task, outcome = self.s.ingest({**self.item, "due_date": "2026-12-05"})
        self.assertEqual(outcome, "updated")
        self.assertEqual((task["due_date"], task["planned_date"]), ("2026-12-05", "2026-12-05"))

    def test_hand_planned_task_keeps_its_plan_when_deadline_moves(self):
        task, _ = self.s.ingest(dict(self.item))
        self.s.update(task["id"], {"planned_date": "2026-11-20"})
        task, _ = self.s.ingest({**self.item, "due_date": "2026-12-05"})
        self.assertEqual(task["planned_date"], "2026-11-20")

    def test_finished_task_is_not_reopened_by_sync(self):
        task, _ = self.s.ingest(dict(self.item))
        self.s.complete(task["id"], "done by hand")
        _, outcome = self.s.ingest(dict(self.item))
        self.assertEqual(outcome, "skipped")
        self.assertEqual(self.s.get(task["id"])["status"], "done")

    def test_user_title_survives_refresh(self):
        task, _ = self.s.ingest(dict(self.item))
        self.s.update(task["id"], {"title": "My better title"})
        task, _ = self.s.ingest({**self.item, "title": "Source title"})
        self.assertEqual(task["title"], "My better title")

    def test_review_groups_by_project(self):
        a = self.s.create("A", project="Acme")
        b = self.s.create("B", project="Globex")
        self.s.complete(a["id"], "cut denials 4%")
        self.s.complete(b["id"])
        t0 = store_mod.today().isoformat()
        r = self.s.review(t0, t0)
        self.assertEqual(r["count"], 2)
        self.assertIn("## Acme (1)", r["markdown"])
        self.assertIn("cut denials 4%", r["markdown"])


class Sync(Temp):
    def test_apply_links_duplicates_and_closes_only_source_tasks(self):
        import sync
        linear, _ = self.s.ingest({"source": "linear", "source_ref": "ENG-9", "title": "Fix checkout"})
        manual = self.s.create("Something I typed")
        found = {
            "tasks": [
                {"source": "slack", "source_ref": "C1:1.0", "title": "Fix checkout please", "priority": "high", "reason": "asked", "same_as_task_id": linear["id"]},
                {"source": "slack", "source_ref": "C1:2.0", "title": "Review deck", "priority": "normal", "reason": "asked", "due_date": None},
            ],
            "completed": [{"task_id": linear["id"], "summary": "Issue closed"}, {"task_id": manual["id"], "summary": "nope"}],
            "summary": "",
        }
        with mock.patch.object(sync.headless, "judge", return_value={"decisions": []}):
            counts = sync.apply(self.s, found, {})
        self.assertEqual((counts["linked"], counts["created"], counts["closed"]), (1, 1, 1))
        self.assertEqual(self.s.get(linear["id"])["status"], "done")
        self.assertEqual(self.s.get(manual["id"])["status"], "todo")  # sync may only close source-backed tasks


class Mcp(unittest.TestCase):
    def test_stdio_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "KARYA_DB": os.path.join(tmp, "m.sqlite3")}
            proc = subprocess.Popen([sys.executable, str(ROOT / "mcp_server.py")], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True, env=env, cwd=ROOT)

            def rpc(i, method, params=None):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code"}})
            self.assertIn("karya_start_task", init["result"]["instructions"])
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            tools = rpc(2, "tools/list")["result"]["tools"]
            self.assertEqual(len(tools), 10)

            def call(i, name, args):
                result = rpc(i, "tools/call", {"name": name, "arguments": args})["result"]
                return result, json.loads(result["content"][0]["text"]) if not result.get("isError") else None

            _, added = call(3, "karya_add_task", {"title": "Fix Acme checkout feedback before next release", "source": "slack", "source_ref": "C9:1.1", "due_date": "fri"})
            self.assertEqual(added["task"]["source"], "slack")
            _, offered = call(4, "karya_start_task", {"title": "Fix checkout feedback for Acme"})
            self.assertFalse(offered["created"])                      # nothing created...
            self.assertTrue(offered["decision_needed"])               # ...until the agent decides
            self.assertEqual(offered["candidates"][0]["id"], added["task"]["id"])
            _, started = call(5, "karya_start_task", {"title": "Fix checkout feedback for Acme", "task_id": added["task"]["id"]})
            self.assertFalse(started["created"])
            self.assertEqual(started["task"]["status"], "doing")
            _, fresh = call(12, "karya_start_task", {"title": "Fix checkout feedback for Acme", "new_task": True})
            self.assertTrue(fresh["created"])
            _, merged = call(13, "karya_merge_tasks", {"keep_id": added["task"]["id"], "drop_id": fresh["task"]["id"]})
            self.assertEqual(merged["task"]["status"], "doing")
            _, done = call(6, "karya_complete_task", {"task_id": added["task"]["id"], "summary": "Fixed 10 of 10 gaps"})
            self.assertEqual(done["task"]["status"], "done")
            _, listing = call(7, "karya_list", {"view": "done_today"})
            self.assertEqual([t["id"] for t in listing["tasks"]], [added["task"]["id"]])
            err, _ = call(8, "karya_complete_task", {"task_id": "nope", "summary": "x"})
            _, mine = call(10, "karya_start_task", {"title": "Plan the weekend hike", "space": "personal"})
            self.assertEqual(mine["task"]["space"], "personal")
            _, work = call(11, "karya_list", {"view": "doing", "space": "work"})
            self.assertNotIn(mine["task"]["id"], [t["id"] for t in work["tasks"]])
            self.assertTrue(err["isError"])
            _, found = call(14, "karya_search", {"query": "checkout feedbak", "status": "done"})
            self.assertEqual([t["id"] for t in found["tasks"]], [added["task"]["id"]])
            self.assertEqual(found["corrected"], {"feedbak": "feedback"})
            self.assertIn("Fixed 10 of 10", found["tasks"][0]["completion_note"])
            _, digest = call(9, "karya_digest", {})
            self.assertEqual(digest["counts"]["done_today"], 1)
            proc.stdin.close()
            proc.wait(timeout=5)


class Http(unittest.TestCase):
    def test_board_quick_add_and_complete(self):
        import server
        with tempfile.TemporaryDirectory() as tmp:
            server.STORE = Store(os.path.join(tmp, "h.sqlite3"))
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"

            def req(method, path, body=None):
                data = json.dumps(body).encode() if body is not None else None
                r = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(r) as resp:
                    return json.loads(resp.read())

            t0 = store_mod.today().isoformat()
            created = req("POST", "/api/tasks", {"quick": "Send deck to Sam !high #Acme_Web due:tomorrow", "planned_date": t0})["task"]
            self.assertEqual((created["title"], created["priority"], created["project"]), ("Send deck to Sam", "high", "Acme Web"))
            self.assertEqual(created["planned_date"], t0)
            board = req("GET", "/api/board")
            self.assertEqual([t["id"] for t in board["tasks"]], [created["id"]])
            req("PATCH", f"/api/tasks/{created['id']}", {"planned_date": None})
            self.assertIsNone(req("GET", f"/api/tasks/{created['id']}")["task"]["planned_date"])
            req("POST", f"/api/tasks/{created['id']}/complete", {"summary": "sent"})
            board = req("GET", "/api/board")
            self.assertEqual([t["id"] for t in board["done"]], [created["id"]])
            with urllib.request.urlopen(base + "/") as resp:
                self.assertIn("kārya".encode(), resp.read())
            httpd.shutdown()


class Notify(Temp):
    def test_digest_text_and_dedup(self):
        import notify
        self.s.create("Overdue thing", due_date=(store_mod.today() - timedelta(days=1)).isoformat(), source="slack", source_url="https://x/y")
        built = notify.build(self.s)
        self.assertIn("[1 overdue](http://127.0.0.1:8788/#overdue)", built["text"])
        self.assertEqual(built["banner"], "1 overdue")
        self.assertIn("[Overdue thing](https://x/y)", built["text"])
        with mock.patch.object(notify, "_via_claude") as post, mock.patch.object(notify, "_macos"):
            first = notify.send(self.s)
            second = notify.send(self.s)
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])  # nothing changed, not the first of the day
        self.assertEqual(post.call_count, 1)


class Morning(Temp):
    """Bot mode: the plan is scheduled in Slack so it arrives with the Mac asleep."""
    cfg = {"slack_bot_token": "xoxb-test", "slack_bot_dm_channel": "D1", "morning_time": "08:00",
           "port": 8788, "macos_notifications": False}

    def at(self, day, hh, mm=0):
        return datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm, tzinfo=store_mod.tz())

    def test_preview_rolls_today_over_and_marks_todays_deadline_overdue(self):
        t0 = store_mod.today()
        task = self.s.create("Finish deck", due_date=t0.isoformat())
        d = self.s.digest(on=t0 + timedelta(days=1))
        self.assertEqual([t["id"] for t in d["overdue"]], [task["id"]])
        self.assertEqual([t["id"] for t in d["planned_today"]], [task["id"]])
        self.assertEqual(self.s.get(task["id"])["planned_date"], t0.isoformat())  # preview changes nothing

    def test_target_times(self):
        import notify
        day = date(2026, 9, 23)
        self.assertEqual(notify._morning_target(self.cfg, self.at(day, 6, 30)), self.at(day, 8))
        self.assertIsNone(notify._morning_target(self.cfg, self.at(day, 12)))
        self.assertEqual(notify._morning_target(self.cfg, self.at(day, 21)), self.at(day + timedelta(days=1), 8))

    def test_evening_schedules_then_replaces_tomorrows_plan(self):
        import notify
        day = date(2026, 9, 23)
        with mock.patch.object(notify.slack_bot, "schedule", side_effect=["S1", "S2"]) as sched, \
             mock.patch.object(notify.slack_bot, "unschedule") as unsched:
            self.assertIn("scheduled for Thu 24 Sep 08:00", notify.morning(self.s, self.cfg, self.at(day, 21)))
            notify.morning(self.s, self.cfg, self.at(day, 22))
        self.assertEqual(sched.call_args_list[0].args[3], int(self.at(day + timedelta(days=1), 8).timestamp()))
        unsched.assert_called_once_with("xoxb-test", "D1", "S1")
        self.assertEqual(self.s.kv_get("morning")["id"], "S2")

    def test_midday_catch_up_sends_once(self):
        import notify
        day = store_mod.today()
        with mock.patch.object(notify.slack_bot, "post", return_value="1.0") as post:
            self.assertEqual(notify.morning(self.s, self.cfg, self.at(day, 11)), "sent now (catch-up)")
            self.assertEqual(notify.morning(self.s, self.cfg, self.at(day, 15)), "already delivered")
        self.assertEqual(post.call_count, 1)

    def test_new_asks_pings_only_for_new_source_tasks(self):
        import notify
        with mock.patch.object(notify.slack_bot, "post", return_value="1.0") as post:
            self.assertIn("baseline", notify.new_asks(self.s, self.cfg))
            self.s.create("typed by hand")
            self.assertEqual(notify.new_asks(self.s, self.cfg), "no new asks")
            self.s.ingest({"source": "slack", "source_ref": "C1:9.9", "title": "Review deck", "priority": "urgent"})
            self.assertEqual(notify.new_asks(self.s, self.cfg), "pinged about 1")
        self.assertIn("Review deck", post.call_args.args[2])

    def test_without_a_bot_the_old_digest_path_runs(self):
        import notify
        with mock.patch.object(notify, "load_config", return_value={"port": 8788, "slack_channel_id": "C0", "macos_notifications": False}), \
             mock.patch.object(notify, "_via_claude") as post:
            out = notify.after_sync(self.s)
        self.assertEqual(out["mode"], "legacy")
        post.assert_called_once()


class Recap(Temp):
    def test_week_recap_lists_outcomes_and_next_deadlines(self):
        import recap
        done = self.s.create("Ship checkout gate", project="Acme")
        self.s.complete(done["id"], "Cut the pending queue from 5.9% to 1.8%")
        self.s.create("Globex add-on rule", due_date=(store_mod.today() + timedelta(days=3)).isoformat())
        start, end = recap.week_bounds(store_mod.today())
        built = recap.build(self.s, start, end)
        self.assertEqual(built["count"], 1)
        self.assertIn("1 done, 1 open", built["subject"])
        self.assertIn("Cut the pending queue from 5.9% to 1.8%", built["html"])
        self.assertIn("Globex add-on rule", built["text"])


class Headless(unittest.TestCase):
    """A reply of 'sent' is not proof; only a successful tool result is."""
    tool = "mcp__claude_ai_Gmail__send_message"

    def fake(self, events):
        out = "\n".join(json.dumps(e) for e in events)
        return mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=out, stderr=""))

    def test_claimed_but_never_called_raises(self):
        import headless
        events = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "sent"}]}},
                  {"type": "result", "subtype": "success", "result": "sent"}]
        with self.fake(events), self.assertRaises(RuntimeError):
            headless.run_tool({"claude_bin": "claude", "notify_model": "haiku"}, self.tool, "x")

    def test_called_and_succeeded_returns_the_result(self):
        import headless
        events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": self.tool, "input": {}}]}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": '{"id":"abc"}'}]}}]
        with self.fake(events):
            self.assertIn("abc", headless.run_tool({"claude_bin": "claude", "notify_model": "haiku"}, self.tool, "x"))

    def test_tool_error_raises(self):
        import headless
        events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": self.tool, "input": {}}]}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "403"}]}}]
        with self.fake(events), self.assertRaises(RuntimeError):
            headless.run_tool({"claude_bin": "claude", "notify_model": "haiku"}, self.tool, "x")


class Catchup(Temp):
    def run_catchup(self, now, last_sync_hours_ago=None, recap_sent=None):
        import catchup
        if last_sync_hours_ago is not None:
            run = self.s.begin_sync(None)
            self.s.finish_sync(run, status="ok")
            stamp = (now.astimezone(timezone.utc) - timedelta(hours=last_sync_hours_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            self.s.db.execute("UPDATE sync_runs SET started_at = ?", (stamp,)); self.s.db.commit()
        if recap_sent:
            self.s.kv_set("recap_week", recap_sent)
        fake_now = mock.MagicMock(wraps=datetime); fake_now.now.return_value = now
        with mock.patch.object(catchup, "Store", return_value=self.s), mock.patch.object(catchup, "datetime", fake_now), \
             mock.patch.object(catchup.subprocess, "run") as run:
            fake_now.fromisoformat = datetime.fromisoformat; fake_now.combine = datetime.combine; fake_now.min = datetime.min
            catchup.main()
        return [c.args[0][-1].rsplit("/", 1)[-1] for c in run.call_args_list]

    def test_stale_sync_is_rerun_fresh_one_is_not(self):
        wed = datetime.now(store_mod.tz()).replace(year=2026, month=9, day=23, hour=10)
        self.assertEqual(self.run_catchup(wed, last_sync_hours_ago=12), ["sync.py"])
        self.assertEqual(self.run_catchup(wed, last_sync_hours_ago=1), [])

    def test_missed_friday_recap_is_sent_once(self):
        sat = datetime.now(store_mod.tz()).replace(year=2026, month=9, day=26, hour=10)
        self.assertEqual(self.run_catchup(sat, last_sync_hours_ago=1), ["recap.py"])
        self.assertEqual(self.run_catchup(sat, last_sync_hours_ago=1, recap_sent="2026-09-21"), [])


class Spaces(Temp):
    """Personal tasks live alongside work but never reach the work record."""

    def test_default_is_work_and_bad_space_is_rejected(self):
        self.assertEqual(self.s.create("x")["space"], "work")
        with self.assertRaises(ValueError):
            self.s.create("y", space="hobby")

    def test_review_and_recap_leave_personal_out(self):
        import recap
        w = self.s.create("Ship gate", project="Acme"); self.s.complete(w["id"], "done")
        p = self.s.create("Book the dentist", space="personal"); self.s.complete(p["id"], "Tuesday 10:00")
        t0 = store_mod.today().isoformat()
        self.assertEqual([t["title"] for g in self.s.review(t0, t0)["groups"] for t in g["tasks"]], ["Ship gate"])
        start, end = recap.week_bounds(store_mod.today())
        built = recap.build(self.s, start, end)
        self.assertNotIn("dentist", built["html"] + built["text"])
        self.assertEqual(len(self.s.done_between(t0, t0)), 2)  # the Done view still has both

    def test_personal_section_only_in_the_private_dm(self):
        import notify
        self.s.create("Fix linkage", priority="urgent")
        self.s.create("Pay credit card bill", due_date="today", space="personal")
        with mock.patch.object(notify, "load_config", return_value={"port": 8788}):
            work_only = notify.build(self.s, "mrkdwn", morning=True)["text"]
            with_mine = notify.build(self.s, "mrkdwn", morning=True, personal=True)["text"]
        self.assertNotIn("credit card", work_only)
        self.assertIn("*Personal*", with_mine)
        self.assertIn("credit card", with_mine)
        self.assertNotIn("1 due today", with_mine)  # header counts are work only

    def test_quick_add_at_personal(self):
        import server
        self.assertEqual(server.parse_quick("Book dentist @personal due:fri")["space"], "personal")
        self.assertNotIn("space", server.parse_quick("Send deck to Sam"))

    def test_old_store_gets_the_column_with_everything_as_work(self):
        path = os.path.join(self.dir.name, "old.sqlite3")
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'todo', priority TEXT NOT NULL DEFAULT 'normal', due_date TEXT, due_time TEXT, planned_date TEXT, project TEXT, source TEXT NOT NULL DEFAULT 'manual', source_ref TEXT, source_url TEXT, requester TEXT, waiting_on TEXT, sort_order REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, completion_note TEXT)")
        c.execute("INSERT INTO tasks(id,title,created_at,updated_at) VALUES ('a','old','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')")
        c.commit(); c.close()
        self.assertEqual(Store(path).get("a")["space"], "work")


class Alarms(Temp):
    """A task with a clock time gets a Slack ping 5 minutes before, even with the Mac asleep."""
    cfg = {"slack_bot_token": "xoxb-test", "slack_bot_dm_channel": "D1", "alarm_lead_minutes": 5}

    def setUp(self):
        super().setUp()
        import alarms
        self.alarms = alarms
        self.now = datetime.now(store_mod.tz()).replace(hour=15, minute=0, second=0, microsecond=0)
        self.sched = mock.patch.object(alarms.slack_bot, "schedule", side_effect=[f"S{i}" for i in range(1, 9)]).start()
        self.unsched = mock.patch.object(alarms.slack_bot, "unschedule").start()
        self.post = mock.patch.object(alarms.slack_bot, "post", return_value="1.0").start()
        self.addCleanup(mock.patch.stopall)
        self.s.listeners.append(lambda st, b, a: alarms.on_change(st, b, a, config=self.cfg, now=self.now))

    def test_time_without_date_means_next_occurrence(self):
        self.assertEqual(store_mod.day_for_time("19:00", now=self.now), self.now.date().isoformat())
        self.assertEqual(store_mod.day_for_time("09:00", now=self.now), (self.now.date() + timedelta(days=1)).isoformat())

    def test_setting_a_time_confirms_and_schedules_five_minutes_before(self):
        t = self.s.create("Order food for dinner", due_date=self.now.date().isoformat(), due_time="19:00", space="personal")
        remind = self.now.replace(hour=18, minute=55)
        self.assertEqual(self.sched.call_args.args[3], int(remind.timestamp()))
        self.assertIn("needs to be done by 7:00 pm", self.sched.call_args.args[2])
        self.assertIn("I'll ping you at 6:55 pm", self.post.call_args.args[2])
        self.assertEqual(self.s.alarm_get(t["id"])["scheduled_id"], "S1")

    def test_moving_the_time_replaces_and_finishing_cancels(self):
        t = self.s.create("Call Sam", due_date=self.now.date().isoformat(), due_time="17:00")
        self.s.update(t["id"], {"due_time": "17:30"})
        self.unsched.assert_called_with("xoxb-test", "D1", "S1")
        self.assertEqual(self.s.alarm_get(t["id"])["scheduled_id"], "S2")
        self.s.update(t["id"], {"title": "Call Sam about the deck"})  # unrelated edit: nothing rescheduled
        self.assertEqual(self.sched.call_count, 2)
        self.s.complete(t["id"], "called")
        self.unsched.assert_called_with("xoxb-test", "D1", "S2")
        self.assertIsNone(self.s.alarm_get(t["id"]))

    def test_deleting_and_clearing_the_time_cancel(self):
        a = self.s.create("A", due_date=self.now.date().isoformat(), due_time="18:00")
        b = self.s.create("B", due_date=self.now.date().isoformat(), due_time="18:30")
        self.s.update(a["id"], {"due_time": None})
        self.s.delete(b["id"])
        self.assertEqual({c.args[2] for c in self.unsched.call_args_list}, {"S1", "S2"})
        self.assertEqual(self.s.alarms(), [])

    def test_inside_the_lead_time_it_reminds_at_once_and_only_once(self):
        t = self.s.create("Leave for the station", due_date=self.now.date().isoformat(), due_time="15:03")
        self.sched.assert_not_called()
        self.assertIn("3 minutes from now", self.post.call_args.args[2])
        self.alarms.reconcile(self.s, config=self.cfg, now=self.now)
        self.assertEqual(self.post.call_count, 1)

    def test_untimed_tasks_never_touch_slack(self):
        self.s.create("Plain task", due_date="tomorrow")
        self.sched.assert_not_called(); self.post.assert_not_called()

    def test_reconcile_adds_what_was_missed_while_the_bot_was_off(self):
        self.s.listeners.clear()  # as if set before the bot existed
        t = self.s.create("Pay rent", due_date=(self.now.date() + timedelta(days=1)).isoformat(), due_time="10:00")
        out = self.alarms.reconcile(self.s, config=self.cfg, now=self.now)
        self.assertEqual(out["changed"], 1)
        self.assertEqual(self.s.alarm_get(t["id"])["scheduled_id"], "S1")
        self.assertEqual(self.alarms.reconcile(self.s, config=self.cfg, now=self.now)["changed"], 0)

    def test_quick_add_reads_times_but_not_ticket_numbers(self):
        import server
        self.assertEqual(server.parse_quick("Order food for dinner before 7pm")["due_time"], "19:00")
        self.assertEqual(server.parse_quick("Order food for dinner before 7pm")["title"], "Order food for dinner")
        self.assertEqual(server.parse_quick("Submit report by 17:00")["due_time"], "17:00")
        for text in ("Fix ENG-2391 for the 250-order batch", "Review PR 6960", "Top 5 orders"):
            self.assertNotIn("due_time", server.parse_quick(text), text)


class Dedup(Temp):
    """The same work from Slack, Linear or a session must end up as one task."""

    def test_slack_thread_matches_in_either_spelling(self):
        t = self.s.create("Fix card declines", source="slack", source_ref="1789983636.393909")
        self.assertEqual(self.s.by_source("slack", "D0TESTDM01:1789983636.393909")["id"], t["id"])
        _, outcome = self.s.ingest({"source": "slack", "source_ref": "D0TESTDM01:1789983636.393909", "title": "Fix Acme card declines"})
        self.assertIn(outcome, ("unchanged", "updated"))
        self.assertEqual(len(self.s.list()), 1)

    def test_linked_reference_is_remembered_by_the_next_sync(self):
        linear, _ = self.s.ingest({"source": "linear", "source_ref": "ENG-2419", "title": "Fix >4-item invoice lines"})
        self.assertTrue(self.s.add_ref(linear["id"], "slack", "C1:1790.1"))
        _, outcome = self.s.ingest({"source": "slack", "source_ref": "C1:1790.1", "title": "can you cap the invoice items?"})
        self.assertNotEqual(outcome, "created")
        self.assertEqual(len(self.s.list()), 1)

    def test_one_linear_issue_per_task(self):
        a, _ = self.s.ingest({"source": "linear", "source_ref": "ENG-1", "title": "A"})
        b, _ = self.s.ingest({"source": "linear", "source_ref": "ENG-2", "title": "B"})
        self.assertFalse(self.s.add_ref(a["id"], "linear", "ENG-2"))
        with self.assertRaises(ValueError):
            self.s.merge(a["id"], b["id"])

    def test_merge_keeps_history_progress_and_references(self):
        session = self.s.create("New signup flow by region", source="claude", status="doing", notes="EU means the consent banner")
        self.s.update(session["id"], {"status": "waiting", "waiting_on": "PR review"})
        linear, _ = self.s.ingest({"source": "linear", "source_ref": "ENG-2427", "title": "New signup identification"})
        kept = self.s.merge(linear["id"], session["id"])
        self.assertEqual((kept["status"], kept["waiting_on"]), ("waiting", "PR review"))
        self.assertIn("consent banner", kept["notes"])
        self.assertEqual(self.s.get(session["id"])["status"], "dropped")
        self.assertIn("started", [e["kind"] for e in self.s.events(linear["id"])])  # the session's history moved over

    def test_sync_links_a_slack_ask_that_names_the_linear_id(self):
        import sync
        linear, _ = self.s.ingest({"source": "linear", "source_ref": "ENG-2419", "title": "Fix >4-item invoice lines"})
        found = {"tasks": [{"source": "slack", "source_ref": "C2:5.5", "title": "Any update on the pointer cap?",
                            "priority": "high", "reason": "Alex: status on ENG-2419?"}], "completed": [], "summary": ""}
        with mock.patch.object(sync.headless, "judge") as judge:
            counts = sync.apply(self.s, found, {})
        judge.assert_not_called()
        self.assertEqual(counts["linked"], 1)
        self.assertEqual(self.s.by_source("slack", "C2:5.5")["id"], linear["id"])

    def test_sync_asks_the_judge_about_look_alikes_and_follows_it(self):
        import sync
        session = self.s.create("Globex: signup identification by region + EU consent vs cookie banner", source="claude", status="doing")
        found = {"tasks": [
            {"source": "linear", "source_ref": "ENG-2427", "title": "New signup identification", "priority": "urgent",
             "reason": "identify new signups by region, not plan name", "state": "doing"},
            {"source": "linear", "source_ref": "ENG-2500", "title": "Unrelated refund report", "priority": "normal", "reason": "x"},
        ], "completed": [], "summary": ""}
        verdict = {"decisions": [{"item": 0, "same_as_task_id": session["id"], "reason": "same signup work"},
                                 {"item": 1, "same_as_task_id": None, "reason": "different"}]}
        with mock.patch.object(sync.headless, "judge", return_value=verdict) as judge:
            counts = sync.apply(self.s, found, {})
        judge.assert_called_once()
        self.assertEqual((counts["linked"], counts["created"]), (1, 1))
        self.assertEqual(self.s.by_source("linear", "ENG-2427")["id"], session["id"])

    def test_judge_failure_creates_rather_than_loses(self):
        import sync
        self.s.create("Something typed by hand")
        found = {"tasks": [{"source": "slack", "source_ref": "C3:1.1", "title": "Something typed by hand, please", "priority": "normal", "reason": "x"}],
                 "completed": [], "summary": ""}
        with mock.patch.object(sync.headless, "judge", side_effect=RuntimeError("offline")):
            counts = sync.apply(self.s, found, {})
        self.assertEqual(counts["created"], 1)

    def test_linear_in_progress_shows_as_doing(self):
        import sync
        found = {"tasks": [{"source": "linear", "source_ref": "ENG-9", "title": "Cap invoice items", "priority": "high", "reason": "x", "state": "doing"}],
                 "completed": [], "summary": ""}
        with mock.patch.object(sync.headless, "judge", return_value={"decisions": []}):
            sync.apply(self.s, found, {})
        self.assertEqual(self.s.by_source("linear", "ENG-9")["status"], "doing")

    def test_configured_client_names_do_not_make_tasks_look_alike(self):
        self.s.create("Fix Acme Billing invoice rounding")                 # similarity_ignore: acme, billing
        self.assertEqual(self.s.candidates("Fix Acme Billing login redirect"), [])
        self.assertTrue(self.s.candidates("Fix the invoice rounding again"))


class Search(Temp):
    """"Did we do X, and when?" answered from everything recorded about a task."""

    def ids(self, query, **kw):
        return [t["id"] for t in self.s.search(query, **kw)["tasks"]]

    def test_finds_words_that_only_appear_in_a_log_or_the_outcome(self):
        task = self.s.create("Acme ledger cleanup", source="claude", status="doing")
        self.s.log(task["id"], "Ported the retry gate to the EU region; Priya signed off", actor="claude")
        self.s.complete(task["id"], "Removed duplicate invoices without a matching order on 212 accounts")
        self.s.create("Unrelated deck for Sam")
        found = self.s.search("did we ever fix the duplicate invoices thing?")  # question words are ignored
        self.assertEqual([t["id"] for t in found["tasks"]], [task["id"]])
        hit = found["tasks"][0]
        self.assertEqual((hit["status"], hit["done_date"]), ("done", store_mod.today().isoformat()))
        self.assertEqual(hit["hit"]["label"], "Outcome")
        self.assertIn("\x02invoices\x03", hit["hit"]["text"])
        log_hit = self.s.search("priya")["tasks"][0]["hit"]
        self.assertEqual((log_hit["label"], log_hit["date"]), ("Progress", store_mod.today().isoformat()))

    def test_codes_ids_and_the_tracking_task_first(self):
        tracker, _ = self.s.ingest({"source": "linear", "source_ref": "ENG-2430", "title": "Triage checkout errors"})
        sub = self.s.create("ENG-2430 2b: refunds over 100 become credits")
        other = self.s.create("Check the v2.3.9 retry change on ERR-504 (order 40418273991562)")
        self.assertEqual(self.ids("ENG-2430"), [tracker["id"], sub["id"]])  # the task tracking the issue leads
        self.assertEqual(self.ids("eng 2430")[0], tracker["id"])            # dictated without the hyphen
        self.assertEqual(self.ids("v2.3.9"), [other["id"]])
        self.assertEqual(self.ids("v2.3.1"), [])                            # a version is not "close enough"
        self.assertEqual(self.ids("40418273991562"), [other["id"]])
        self.assertEqual(self.s.search("ENG-2430")["tasks"][0]["refs"], ["ENG-2430"])

    def test_typos_prefixes_and_synonyms(self):
        tag = self.s.create("Tagging the 150 checkout orders for Sam")
        auth = self.s.create("Fix R4 auth token refresh")
        self.s.create("Authentication gate v1.18")
        pr = self.s.create("Review the pull request for the invoice API")
        found = self.s.search("Zanzibar tagging")
        self.assertEqual(found["missing"], ["Zanzibar"])                   # in no task: said, not guessed
        self.assertEqual([t["id"] for t in found["tasks"]], [tag["id"]])
        self.assertEqual(self.s.search("taging")["corrected"], {"taging": "tagging"})
        self.assertEqual(self.ids("tagg"), [tag["id"]])                    # half-typed, the index holds stems
        self.assertIn(auth["id"], self.ids("auth"))
        self.assertEqual(len(self.ids("authentication")), 2)               # config.json synonym: auth = authentication
        self.assertEqual(self.ids("pr"), [pr["id"]])                       # built in: PR = pull request

    def test_every_word_first_then_most_of_them(self):
        both = self.s.create("Retry limit 25 on Kafka consumers")
        scattered = self.s.create("Kafka consumer edits", notes="retry questions")
        self.s.log(scattered["id"], "25 of 30 consumers checked")
        one = self.s.create("Retry limit 24 backoff period")
        found = self.s.search("retry 25 kafka")
        self.assertEqual([t["id"] for t in found["tasks"]][:2], [both["id"], scattered["id"]])
        self.assertFalse(found["tasks"][0]["partial"])
        self.assertTrue(all(t["partial"] for t in found["tasks"] if t["id"] == one["id"]))

    def test_bookkeeping_is_not_searched_and_duplicates_are_hidden(self):
        a = self.s.create("Rotate API keys monthly", planned_date="yesterday")
        self.s.rollover()                                                 # "Moved from ... to today"
        self.s.update(a["id"], {"planned_date": "tomorrow"})              # "planned date → ..."
        self.assertEqual(self.ids("today"), [])
        self.assertEqual(self.ids("moved"), [])
        dup = self.s.create("Rotate API keys monthly again")
        self.s.merge(a["id"], dup["id"])
        self.assertEqual(self.ids("rotate"), [a["id"]])

    def test_status_filter_and_counts(self):
        done = self.s.create("Webhook retry rule for Globex")
        self.s.complete(done["id"], "Shipped in PR #7019")
        todo = self.s.create("Webhook signing question for Priya")
        found = self.s.search("webhook", status="open")
        self.assertEqual(([t["id"] for t in found["tasks"]], found["counts"]), ([todo["id"]], {"all": 2, "open": 1, "done": 1}))
        self.assertEqual(self.ids("7019", status="done"), [done["id"]])
        with self.assertRaises(ValueError):
            self.s.search("x", status="dropped")

    def test_sees_writes_from_another_process(self):
        self.assertEqual(self.ids("gateway"), [])
        other = Store(self.db)                                            # the MCP server, in another process
        task = other.create("Replica runner gateway timeout")
        self.assertEqual(self.ids("gateway"), [task["id"]])
        other.log(task["id"], "fill_missing.py reruns the jobs that 404ed")
        self.assertEqual(self.ids("404ed"), [task["id"]])
        other.delete(task["id"])
        self.assertEqual(self.ids("gateway"), [])
        other.close()

    def test_http_search(self):
        import server
        server.STORE = self.s
        task = self.s.create("Kārya search box")
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/api/search?q=karya&status=open"
            with urllib.request.urlopen(url) as resp:
                body = json.loads(resp.read())
            self.assertEqual([t["id"] for t in body["tasks"]], [task["id"]])  # diacritics folded
            self.assertEqual(body["counts"]["open"], 1)
        finally:
            httpd.shutdown()
            httpd.server_close()


class Backfill(Temp):
    """Work from before Kārya was recording, imported from git with its real dates."""

    def pr(self, title="Acme checkout batch: retry gate", **kw):
        base = dict(source="github", source_ref="app#5847", status="done", created_at="2026-07-24T12:56:00Z",
                    completed_at="2026-07-29T10:28:00Z", project="Acme",
                    completion_note="Merged into main on 29 Jul 2026 as PR #5847 (app, branch fix/acme-retry).",
                    notes="Retry gate for checkout; also touches ENG-2430 wording",
                    history=[("2026-07-24T12:56:00Z", "created", "Opened PR #5847 in app from branch fix/acme-retry"),
                             ("2026-07-29T10:28:00Z", "completed", "Merged into dev")])
        base.update(kw)
        return self.s.import_record(title, **base)

    def test_lands_on_the_day_it_was_done_with_its_history(self):
        task, created = self.pr()
        self.assertTrue(created)
        self.assertEqual((task["status"], task["done_date"], task["created_at"]), ("done", "2026-07-29", "2026-07-24T12:56:00Z"))
        self.assertEqual([t["id"] for t in self.s.board("2026-07-29", "2026-07-29")["done"]], [task["id"]])
        self.assertEqual([e["at"][:10] for e in self.s.events(task["id"])], ["2026-07-29", "2026-07-24"])
        found = self.s.search("retry gate")["tasks"][0]
        self.assertEqual((found["id"], found["done_date"]), (task["id"], "2026-07-29"))
        self.assertIn("fix/acme-retry", " ".join(u["text"] for u in [found["hit"], found["context"]] if u))

    def test_is_idempotent_and_never_claims_a_linear_issue(self):
        first, _ = self.pr()
        again, created = self.pr(title="changed")
        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertIsNone(self.s.by_source("linear", "ENG-2430"))  # mentioned in the notes, not owned
        live, outcome = self.s.ingest({"source": "linear", "source_ref": "ENG-2430", "title": "Triage checkout errors"})
        self.assertEqual(outcome, "created")                          # the sync still makes the live task

    def test_rejects_an_unknown_status(self):
        with self.assertRaises(ValueError):
            self.pr(status="merged")

    def test_helpers(self):
        import backfill_git as b
        self.assertEqual(b.clean_title("fix(billing): round invoices when tax is zero"), "Round invoices when tax is zero")
        b.SETTINGS = {"path_projects": {"billing": "Billing", "web": "Website"}, "title_projects": {"ios": "Mobile"},
                      "repo_projects": {"api": "Platform"}}
        self.assertEqual(b.project_for(["services/billing/tax.py"], "", "you/app"), "Billing")
        self.assertEqual(b.project_for(["apps/web/a.ts", "apps/web/b.ts", "services/billing/x.py"], "", "x"), "Website")
        self.assertEqual(b.project_for([], "fix(ios): keep the cart", "you/app"), "Mobile")
        self.assertEqual(b.project_for([], "fix(workflows): keep modifiers", "you/api"), "Platform")
        self.assertEqual(b.project_for([], "anything", "you/app"), "app")
        b.SETTINGS = {}
        self.assertEqual(b.summary("## Summary\n\nFixes the **retry** gate. Second sentence.\n\n| a |"),
                         "Fixes the retry gate. Second sentence.")
        self.assertEqual(b.utc("2026-09-23T10:37:05+05:30"), "2026-09-23T05:07:05Z")

    def test_a_pr_goes_to_the_task_that_is_its_work(self):
        import backfill_git as b
        weekend = self.s.create("Weekend pricing rule", status="waiting", waiting_on="Review/merge PR #7040")
        self.s.log(weekend["id"], "Opened PR #7040 for weekend pricing")
        triage = self.s.create("Globex ENG-2430: triage checkout errors", project="Globex")
        self.s.log(triage["id"], "Shipped: #7009, #7019, #7039 and #7040")
        named = self.s.create("Globex 2b: refunds over 100 become credits", project="Globex")
        self.s.complete(named["id"], "PR #7009 merged to dev 24 Sep")
        mentions = b.Mentions(self.s)
        self.assertEqual(mentions.for_pr(7040, "Globex", "Globex: weekend pricing (Saturday and Sunday rates)")["id"], weekend["id"])
        self.assertEqual(mentions.for_pr(7009, "Globex", "Globex: credits for large refunds, retry diagnostics")["id"], named["id"])
        self.assertIsNone(mentions.for_pr(6999, "Globex", "anything"))


if __name__ == "__main__":
    unittest.main()
