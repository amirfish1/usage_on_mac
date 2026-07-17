#!/usr/bin/env python3
"""Usage burn-down goals for the Claude usage menu-bar plugin.

Set a goal — a target weekly-usage percent by a deadline, optionally with
per-model-family share floors — and the dropdown renders a burn-down line:
current vs required pace, on-track / behind, projected percent at deadline.

    ./goal.py "100% by 2026-07-17T10:00+03:00, fable>=50%"
    ./goal.py --target 100 --by 2026-07-17T10:00 --model-share fable=50
    ./goal.py --show
    ./goal.py --clear

Config lives in ~/.cache/claude-usage-goal.json. Also importable:
`load_goal()` + `goal_status(goal, weekly_pct, elapsed_h, shares)`.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

GOAL_FILE = Path(os.environ.get("CLAUDE_USAGE_GOAL_FILE")
                 or Path.home() / ".cache" / "claude-usage-goal.json")

# Work-window pace model — keep in sync with claude-usage.5m.py.
WORK_START_HOUR = 7
WORK_END_HOUR = 20


def work_hours_between(start_local, end_local,
                       h_start=WORK_START_HOUR, h_end=WORK_END_HOUR):
    """Work hours within [start_local, end_local] given the daily window."""
    if end_local <= start_local:
        return 0.0
    total = 0.0
    cur = start_local.date()
    last = end_local.date()
    tz = start_local.tzinfo
    while cur <= last:
        ws = datetime.combine(cur, dtime(h_start, 0), tzinfo=tz)
        we = datetime.combine(cur, dtime(h_end, 0), tzinfo=tz)
        s = max(ws, start_local)
        e = min(we, end_local)
        if e > s:
            total += (e - s).total_seconds() / 3600.0
        cur += timedelta(days=1)
    return total


def parse_deadline(text, now_local=None):
    """Accept full ISO (tz optional → local), or 'HH:MM' → next occurrence."""
    now_local = now_local or datetime.now().astimezone()
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        dl = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dl <= now_local:
            dl += timedelta(days=1)
        return dl
    dl = datetime.fromisoformat(text)
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=now_local.tzinfo)
    return dl


def parse_goal_string(text, now_local=None):
    """Parse '100% by 2026-07-17T10:00+03:00, fable>=50%' into a goal dict."""
    goal = {"target_pct": None, "deadline": None, "model_share": {}}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(
            r"(?i)(\d+(?:\.\d+)?)\s*%?\s+by\s+(.+)", part)
        if m:
            goal["target_pct"] = float(m.group(1))
            goal["deadline"] = parse_deadline(m.group(2), now_local)
            continue
        m = re.fullmatch(r"(?i)([a-z]+)\s*>=\s*(\d+(?:\.\d+)?)\s*%?", part)
        if m:
            goal["model_share"][m.group(1).lower()] = float(m.group(2))
            continue
        raise ValueError(f"can't parse goal part: {part!r}")
    if goal["target_pct"] is None or goal["deadline"] is None:
        raise ValueError("goal needs '<pct>% by <deadline>'")
    return goal


def save_goal(goal, goal_file=None):
    goal_file = Path(goal_file) if goal_file else GOAL_FILE
    goal_file.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(goal)
    payload["deadline"] = goal["deadline"].isoformat()
    payload["set_at"] = datetime.now().astimezone().isoformat()
    goal_file.write_text(json.dumps(payload))


def load_goal(goal_file=None):
    """Return the stored goal with a datetime deadline, or None."""
    goal_file = Path(goal_file) if goal_file else GOAL_FILE
    try:
        data = json.loads(goal_file.read_text())
        data["deadline"] = datetime.fromisoformat(data["deadline"])
        data.setdefault("model_share", {})
        return data
    except Exception:
        return None


def clear_goal(goal_file=None):
    goal_file = Path(goal_file) if goal_file else GOAL_FILE
    try:
        goal_file.unlink()
    except FileNotFoundError:
        pass


def goal_status(goal, weekly_pct, elapsed_h, shares=None, now_local=None):
    """Burn-down math for the dropdown.

    weekly_pct — current weekly usage %; elapsed_h — work hours burned so far
    this week (drives the avg pace); shares — {family: fraction 0..1} of
    local burn (from usage_by_model), for model-share floors.

    Returns a dict: hours_to_deadline (work hours), required_pace and
    current_pace (pp per work hour), projected_pct at the deadline,
    on_track / expired / achieved, and per-family share verdicts.
    """
    now_local = now_local or datetime.now().astimezone()
    deadline = goal["deadline"]
    target = goal["target_pct"]
    expired = now_local >= deadline
    achieved = weekly_pct is not None and weekly_pct >= target

    hours_left = work_hours_between(now_local, deadline)
    current_pace = (weekly_pct / elapsed_h) if (
        weekly_pct is not None and elapsed_h and elapsed_h > 0) else None
    required_pace = None
    if weekly_pct is not None and not expired and not achieved and hours_left > 0:
        required_pace = (target - weekly_pct) / hours_left
    projected = None
    if weekly_pct is not None and current_pace is not None and not expired:
        projected = weekly_pct + current_pace * hours_left

    on_track = None
    if achieved:
        on_track = True
    elif projected is not None:
        on_track = projected >= target

    model_status = {}
    for family, floor_pct in (goal.get("model_share") or {}).items():
        cur = None if shares is None else (shares.get(family, 0.0) * 100)
        model_status[family] = {
            "target_pct": floor_pct,
            "current_pct": cur,
            "met": (cur is not None and cur >= floor_pct),
        }

    return {
        "target_pct": target,
        "deadline": deadline,
        "expired": expired,
        "achieved": achieved,
        "hours_to_deadline": hours_left,
        "current_pace": current_pace,
        "required_pace": required_pace,
        "projected_pct": projected,
        "on_track": on_track,
        "model_share": model_status,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("goal", nargs="?", help='e.g. "100%% by 2026-07-17T10:00, fable>=50%%"')
    ap.add_argument("--target", type=float, help="target weekly percent")
    ap.add_argument("--by", help="deadline (ISO datetime or HH:MM)")
    ap.add_argument("--model-share", action="append", default=[],
                    metavar="FAMILY=PCT", help="share floor, e.g. fable=50 (repeatable)")
    ap.add_argument("--show", action="store_true", help="print the current goal")
    ap.add_argument("--clear", action="store_true", help="remove the goal")
    args = ap.parse_args(argv)

    if args.clear:
        clear_goal()
        print("goal cleared")
        return 0
    if args.show:
        goal = load_goal()
        print(json.dumps(
            {**goal, "deadline": goal["deadline"].isoformat()}, indent=1)
            if goal else "no goal set")
        return 0

    if args.goal:
        goal = parse_goal_string(args.goal)
    elif args.target is not None and args.by:
        shares = {}
        for spec in args.model_share:
            fam, _, pct = spec.partition("=")
            shares[fam.strip().lower()] = float(pct)
        goal = {"target_pct": args.target,
                "deadline": parse_deadline(args.by),
                "model_share": shares}
    else:
        ap.error("give a goal string, or --target and --by")

    save_goal(goal)
    shares = " ".join(f"{f}>={p:.0f}%" for f, p in goal["model_share"].items())
    print(f"goal set: {goal['target_pct']:.0f}% by "
          f"{goal['deadline'].strftime('%Y-%m-%d %H:%M %Z')}"
          + (f" · {shares}" if shares else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
