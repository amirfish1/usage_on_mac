#!/usr/bin/env python3
"""Per-session context-fill snapshot for active Claude Code sessions.

Usage:
  ccc-context-fill.py                  # JSON to stdout, sessions modified in last 60 min
  ccc-context-fill.py --max-age 30     # 30-min window
  ccc-context-fill.py --table          # human-readable table
  ccc-context-fill.py --only-warning   # only sessions ≥70% (compact_soon or expensive)
  ccc-context-fill.py --pretty         # indent JSON

For CCC integration: parse the JSON array. Each entry has fill_pct, flag,
session_id, project_dir, model, context_window, context_loaded,
last_turn_at, minutes_since_last_turn.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_lib import scan_sessions  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-age", type=int, default=60, help="minutes")
    p.add_argument("--pretty", action="store_true")
    p.add_argument("--table", action="store_true")
    p.add_argument("--only-warning", action="store_true",
                   help="only include sessions flagged compact_soon or expensive")
    args = p.parse_args()

    sessions = scan_sessions(args.max_age)
    if args.only_warning:
        sessions = [s for s in sessions if s["flag"] != "ok"]

    if args.table:
        print(f"{'fill%':>6}  {'flag':<13} {'mins':>5}  {'model':<22} {'session':<10} project")
        for s in sessions:
            mins = s["minutes_since_last_turn"]
            mins_s = str(mins) if mins is not None else "-"
            print(f"{s['fill_pct']:>5.1f}%  {s['flag']:<13} "
                  f"{mins_s:>5}  {(s['model'] or '?')[:22]:<22} "
                  f"{s['session_id'][:8]:<10} {s['project_dir']}")
    else:
        print(json.dumps(sessions, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
