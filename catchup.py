#!/usr/bin/env python3
"""Run at every login (launchd ai.karya.catchup, RunAtLoad).

launchd replays a calendar slot missed while the Mac was *asleep*, but not one
missed while it was *shut down*. So after a restart this does what the missed
slots would have done:
* sync, if the last good sync is more than 6 hours old (the sync then
  schedules or sends the morning plan and pings about new asks as usual);
* send the weekly recap, if Friday 18:30 has passed this week and it hasn't
  gone yet.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import recap
from store import ROOT, Store, tz

STALE = timedelta(hours=6)


def main() -> int:
    store = Store()
    now = datetime.now(tz())
    last = store.last_good_sync()
    age = now - datetime.fromisoformat(last["started_at"].replace("Z", "+00:00")).astimezone(tz()) if last else None
    if age is None or age > STALE:
        print(f"[{now:%Y-%m-%d %H:%M}] last sync {'never' if age is None else f'{age.total_seconds() / 3600:.1f}h ago'}: syncing", flush=True)
        subprocess.run([sys.executable, str(ROOT / "sync.py")], cwd=ROOT)
    else:
        print(f"[{now:%Y-%m-%d %H:%M}] last sync {age.total_seconds() / 3600:.1f}h ago: no catch-up needed", flush=True)
    try:
        import alarms
        print(f"alarms: {alarms.reconcile(store)}", flush=True)  # anything scheduled while Slack was unreachable
    except Exception as exc:  # noqa: BLE001
        print(f"alarms: reconcile failed: {exc}", flush=True)
    start, _ = recap.week_bounds(now.date())
    friday_evening = datetime.combine(start + timedelta(days=4), datetime.min.time(), tz()).replace(hour=18, minute=30)
    if now >= friday_evening and store.kv_get("recap_week") != start.isoformat():
        print("this week's recap was missed: sending it", flush=True)
        subprocess.run([sys.executable, str(ROOT / "recap.py")], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
