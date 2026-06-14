#!/usr/bin/env python3
# <swiftbar.title>Claude Usage</swiftbar.title>
# <swiftbar.author>amir</swiftbar.author>
# <swiftbar.desc>Real Anthropic Pro/Max weekly usage % via the user's logged-in Chrome session.</swiftbar.desc>
# <swiftbar.dependencies>python3, Google Chrome Beta with claude.ai logged in</swiftbar.dependencies>
#
# Refresh: 5m (filename: claude-usage.5m.py)
#
# How it works:
#   This plugin shells out to fetch-usage.applescript, which asks Chrome Beta to run
#   fetch('/api/organizations/<id>/usage') in an already-open, already-logged-in
#   claude.ai tab. The endpoint returns the same numbers shown in claude.ai/settings/usage:
#     - five_hour          — current 5h session %
#     - seven_day          — weekly limit, all models %
#     - seven_day_sonnet   — weekly limit, Sonnet only %
#     - extra_usage        — $ overage spent vs monthly cap
#
# One-time setup required:
#   Chrome Beta → View → Developer → "Allow JavaScript from Apple Events"
#   Keep at least one claude.ai tab open in Chrome Beta, logged in.
#
# Cache: last successful response is saved to ~/.cache/claude-usage-pct.json so
# the menu still shows something if Chrome is closed temporarily.

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path

SCRIPT = Path(__file__).resolve()
APPLESCRIPT = SCRIPT.parent / "fetch-usage.applescript"
CACHE = Path.home() / ".cache" / "claude-usage-pct.json"
CAL_FILE = Path.home() / ".cache" / "claude-usage-cal.json"
TOKEN_CACHE = Path.home() / ".cache" / "claude-usage-tokens.json"
# Manual override for the pace "week start" (see get_week_start). Set/cleared
# from the menu bar; auto-expires when the weekly reset rolls to a new date.
WEEKSTART_FILE = Path.home() / ".cache" / "claude-usage-weekstart.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
STALE_AFTER = 600  # seconds — show a "stale" marker if older than this

# Pace model: assume usage accrues only during your daily work window.
# Defaults: 7am–8pm local, 7 days/week → 13×7 = 91 "work hours" per week.
# Tweak as your real rhythm shifts.
WORK_START_HOUR = 7
WORK_END_HOUR = 20  # exclusive (so 20 means "stop at 8pm")
WORK_DAYS_PER_WEEK = 7


def fetch_via_chrome():
    try:
        proc = subprocess.run(
            ["osascript", str(APPLESCRIPT)],
            # The AppleScript may reload a Memory-Saver-frozen tab and retry (~25s worst case)
            capture_output=True, text=True, timeout=35,
        )
    except Exception as e:
        return None, f"osascript failed: {e}"
    out = (proc.stdout or "").strip()
    if not out:
        return None, (proc.stderr or "").strip() or "empty response"
    try:
        parsed = json.loads(out)
    except Exception as e:
        return None, f"bad json: {e}"
    if not parsed.get("ok"):
        return None, parsed.get("error", "unknown")
    return parsed["usage"], None


def write_cache(usage):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"usage": usage, "fetched_at": time.time()}))
    except Exception:
        pass


def read_cache():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return None


def fmt_reset(iso_str):
    if not iso_str:
        return ""
    try:
        t = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = t - datetime.now(timezone.utc)
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "due now"
        h, rem = divmod(secs, 3600)
        m, _ = divmod(rem, 60)
        if h >= 24:
            d, h = divmod(h, 24)
            return f"{d}d {h}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return iso_str


def status_for(pct):
    if pct is None:
        return "⚪"
    if pct < 50:
        return "🟢"
    if pct < 80:
        return "🟡"
    if pct < 100:
        return "🟠"
    return "🔴"


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_weekstart_override():
    try:
        return json.loads(WEEKSTART_FILE.read_text())
    except Exception:
        return None


def resets_week_key(resets_at_iso):
    """A stable key identifying the reset week. Uses the UTC date of resets_at:
    the reset lands near local midnight, so its *local* date can flip across the
    day boundary on sub-second jitter between fetches — its UTC date (reset is
    ~07:00 UTC, far from a UTC boundary) does not."""
    return parse_iso(resets_at_iso).astimezone(timezone.utc).date().isoformat()


def active_weekstart_override(resets_at_iso):
    """Return the manual week-start datetime if an override applies to the
    current reset week, else None. Keyed on resets_week_key so the override
    auto-expires once Anthropic's weekly reset rolls over."""
    ov = load_weekstart_override()
    if not ov or not resets_at_iso:
        return None
    try:
        if ov.get("applies_to_resets_week") == resets_week_key(resets_at_iso):
            return parse_iso(ov["week_start"]).astimezone()
    except Exception:
        return None
    return None


def get_week_start(resets_at_iso):
    if not resets_at_iso:
        return None
    try:
        resets_local = parse_iso(resets_at_iso).astimezone()
    except Exception:
        return None
    override = active_weekstart_override(resets_at_iso)
    if override is not None:
        return override
    return resets_local - timedelta(days=7)



def elapsed_work_hours(start_local, now_local, h_start, h_end):
    """Total work hours within [start_local, now_local] given a daily window."""
    if now_local <= start_local:
        return 0.0
    total = 0.0
    cur = start_local.date()
    end = now_local.date()
    tz = start_local.tzinfo
    while cur <= end:
        ws = datetime.combine(cur, dtime(h_start, 0), tzinfo=tz)
        we = datetime.combine(cur, dtime(h_end, 0), tzinfo=tz)
        s = max(ws, start_local)
        e = min(we, now_local)
        if e > s:
            total += (e - s).total_seconds() / 3600.0
        cur += timedelta(days=1)
    return total


def count_week_tokens(week_start_local):
    """Sum input+output+cache tokens from JSONL transcripts since week_start_local.
    Uses a per-file (mtime,size) cache so the active session is the only file reparsed."""
    cutoff_utc_iso = week_start_local.astimezone(timezone.utc).isoformat()
    try:
        cache = json.loads(TOKEN_CACHE.read_text())
    except Exception:
        cache = {}
    new_cache = {"_v": 1, "_week": week_start_local.isoformat()}
    if cache.get("_week") != new_cache["_week"]:
        cache = {}  # week changed → invalidate

    total = 0
    seen = set()
    cutoff_mtime = (week_start_local - timedelta(days=1)).timestamp()
    for path in glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < cutoff_mtime:
            continue
        sig = f"{int(st.st_mtime)}:{st.st_size}"
        prev = cache.get(path)
        if prev and prev.get("sig") == sig:
            entries = prev["entries"]
        else:
            entries = []
            try:
                with open(path, "r", errors="ignore") as f:
                    for line in f:
                        if '"assistant"' not in line:
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get("type") != "assistant":
                            continue
                        ts = rec.get("timestamp")
                        if not ts or ts < cutoff_utc_iso:
                            continue
                        msg = rec.get("message") or {}
                        msg_id = msg.get("id")
                        usage = msg.get("usage") or {}
                        if not msg_id or not usage:
                            continue
                        toks = (
                            (usage.get("input_tokens") or 0)
                            + (usage.get("output_tokens") or 0)
                            + (usage.get("cache_read_input_tokens") or 0)
                            + (usage.get("cache_creation_input_tokens") or 0)
                        )
                        if toks > 0:
                            entries.append([msg_id, toks])
            except Exception:
                pass
        new_cache[path] = {"sig": sig, "entries": entries}
        for msg_id, toks in entries:
            if msg_id in seen:
                continue
            seen.add(msg_id)
            total += toks

    try:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps(new_cache))
    except Exception:
        pass
    return total


def save_calibration(week_start_local, tokens, real_pct):
    try:
        CAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        CAL_FILE.write_text(json.dumps({
            "week_start": week_start_local.isoformat(),
            "tokens": tokens,
            "real_pct": real_pct,
            "calibrated_at": time.time(),
        }))
    except Exception:
        pass


def load_calibration():
    try:
        return json.loads(CAL_FILE.read_text())
    except Exception:
        return None


def pace_for(weekly_pct, weekly_resets_at_iso):
    """Returns dict with elapsed_h, total_h, expected_pct, delta_pp, projected_pct, or None."""
    if weekly_pct is None or not weekly_resets_at_iso:
        return None
    week_start_local = get_week_start(weekly_resets_at_iso)
    if not week_start_local:
        return None
    try:
        resets_local = parse_iso(weekly_resets_at_iso).astimezone()
    except Exception:
        return None
    now_local = datetime.now().astimezone()
    total_h = elapsed_work_hours(week_start_local, resets_local, WORK_START_HOUR, WORK_END_HOUR)
    elapsed_h = elapsed_work_hours(week_start_local, now_local, WORK_START_HOUR, WORK_END_HOUR)
    expected = (elapsed_h / total_h) * 100 if total_h else 0
    delta = weekly_pct - expected
    projected = (weekly_pct / elapsed_h) * total_h if elapsed_h > 0 else None
    hours_left = max(0.0, total_h - elapsed_h)
    return {
        "elapsed_h": elapsed_h,
        "total_h": total_h,
        "hours_left": hours_left,
        "expected_pct": expected,
        "delta_pp": delta,
        "projected_pct": projected,
        "week_start": week_start_local,
    }


def _osascript_dialog(prompt, default_answer):
    """Show a text-input dialog. Returns the entered text, or None if the user
    cancelled or anything went wrong."""
    try:
        proc = subprocess.run(
            ["osascript", "-e",
             f'display dialog {json.dumps(prompt)} default answer {json.dumps(default_answer)} '
             f'with title "Claude Usage — pace start"'],
            capture_output=True, text=True, timeout=180,
        )
    except Exception:
        return None
    if proc.returncode != 0:  # user pressed Cancel
        return None
    out = proc.stdout or ""
    marker = "text returned:"
    idx = out.find(marker)
    if idx == -1:
        return None
    return out[idx + len(marker):].strip()


def _osascript_alert(msg):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display dialog {json.dumps(msg)} with title "Claude Usage" '
             f'buttons {{"OK"}} default button "OK" with icon caution'],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def cli_set_start():
    """Prompt for a new pace week-start and persist it as an override."""
    cached = read_cache()
    resets_at = None
    if cached:
        resets_at = ((cached.get("usage") or {}).get("seven_day") or {}).get("resets_at")
    if not resets_at:
        _osascript_alert("No weekly usage data cached yet. Open the menu once to "
                         "fetch live data, then try again.")
        return
    resets_local = parse_iso(resets_at).astimezone()
    cur_ws = get_week_start(resets_at) or (resets_local - timedelta(days=7))
    default = cur_ws.strftime("%Y-%m-%d %H:%M")
    prompt = ("Set the pace week-start (local time).\n"
              f"Weekly resets: {resets_local.strftime('%Y-%m-%d %H:%M')}.\n"
              "Format: YYYY-MM-DD HH:MM")
    ans = _osascript_dialog(prompt, default)
    if not ans:
        return  # cancelled
    try:
        ws = datetime.strptime(ans, "%Y-%m-%d %H:%M").astimezone()
    except Exception:
        _osascript_alert(f"Couldn't parse '{ans}'.\nUse format YYYY-MM-DD HH:MM "
                         "(e.g. 2026-06-12 06:00).")
        return
    if ws >= resets_local:
        _osascript_alert("Start time must be before the weekly reset "
                         f"({resets_local.strftime('%Y-%m-%d %H:%M')}).")
        return
    try:
        WEEKSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        WEEKSTART_FILE.write_text(json.dumps({
            "week_start": ws.isoformat(),
            "applies_to_resets_at": resets_at,
            "applies_to_resets_week": resets_week_key(resets_at),
            "set_at": time.time(),
        }))
    except Exception as e:
        _osascript_alert(f"Couldn't save override: {e}")


def cli_clear_start():
    try:
        WEEKSTART_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--set-start":
            cli_set_start()
            return
        if sys.argv[1] == "--clear-start":
            cli_clear_start()
            return

    fresh, err = fetch_via_chrome()
    estimated = False

    if fresh is not None:
        write_cache(fresh)
        usage = fresh
        fetched_at = time.time()
        stale = False
        # Calibrate: pair (this week's local tokens) with (real weekly %)
        try:
            sd = (fresh.get("seven_day") or {})
            if sd.get("resets_at") and sd.get("utilization") is not None:
                ws = get_week_start(sd["resets_at"])
                if ws:
                    tokens = count_week_tokens(ws)
                    if tokens > 0:
                        save_calibration(ws, tokens, sd["utilization"])
        except Exception:
            pass
    else:
        cached = read_cache()
        if not cached:
            print("🤖 ?")
            print("---")
            print(f"Can't fetch usage: {err} | color=red")
            print("Need Chrome Beta open with a claude.ai tab logged in.")
            print("Also: Chrome Beta → View → Developer → Allow JavaScript from Apple Events")
            print("---")
            print(f"Refresh | refresh=true")
            return
        usage = cached["usage"]
        fetched_at = cached["fetched_at"]
        stale = (time.time() - fetched_at) > STALE_AFTER

        # Try to extrapolate weekly % from local token delta since calibration
        cal = load_calibration()
        sd = (usage.get("seven_day") or {})
        if cal and sd.get("resets_at"):
            try:
                ws_now = get_week_start(sd["resets_at"])
                if ws_now:
                    cal_ws = datetime.fromisoformat(cal["week_start"])
                    if abs((ws_now - cal_ws).total_seconds()) < 3600 and cal.get("tokens", 0) > 0:
                        rate = cal["real_pct"] / cal["tokens"]  # pct per token
                        cur_tokens = count_week_tokens(ws_now)
                        est = round(cur_tokens * rate, 1)
                        # Replace cached pct with estimate (don't go below cached real %)
                        usage = dict(usage)
                        usage["seven_day"] = dict(sd)
                        usage["seven_day"]["utilization"] = max(est, sd.get("utilization") or 0)
                        estimated = True
                        fetched_at = time.time()
                        stale = False
            except Exception:
                pass

    weekly = (usage.get("seven_day") or {}).get("utilization")
    weekly_reset = (usage.get("seven_day") or {}).get("resets_at")
    ws_override = active_weekstart_override(weekly_reset)
    session = (usage.get("five_hour") or {}).get("utilization")
    session_reset = (usage.get("five_hour") or {}).get("resets_at")
    sonnet = (usage.get("seven_day_sonnet") or {}).get("utilization")
    sonnet_reset = (usage.get("seven_day_sonnet") or {}).get("resets_at")
    opus = (usage.get("seven_day_opus") or {})
    opus_pct = opus.get("utilization") if opus else None
    opus_reset = opus.get("resets_at") if opus else None
    extra = usage.get("extra_usage") or {}

    pace = pace_for(weekly, weekly_reset)

    # Was the live fetch broken on this run?
    fetch_failed = (fresh is None)
    no_tab = bool(err and "no claude.ai tab" in (err or "").lower())

    # Menu bar headline: weekly %, color by projection (the "will I make it" signal)
    if pace and pace["projected_pct"] is not None:
        proj = pace["projected_pct"]
        # Projection thresholds: < 85% = comfortable, < 100 = tight, < 115 = overshoot, else danger
        if proj < 85:    bar_emoji = "🟢"
        elif proj < 100: bar_emoji = "🟡"
        elif proj < 115: bar_emoji = "🟠"
        else:            bar_emoji = "🔴"
        bar = f"{bar_emoji} {weekly:.0f}% · {proj:.0f}% proj"
    else:
        bar = f"{status_for(weekly)} {weekly:.0f}%"
    if estimated:
        bar += " ~est"
    if stale:
        bar += " (stale)"
    # Stale > 1 hour or fetch failure → louder warning prefix
    cache_age = time.time() - fetched_at
    if fetch_failed and cache_age > 3600:
        bar = "⚠️ " + bar
    elif fetch_failed:
        bar = "⚠ " + bar
    print(f"{bar} | size=12")
    print("---")

    # If live fetch is broken, surface the cause + a one-click fix at the top of the dropdown
    if fetch_failed:
        cause = "no claude.ai tab in Chrome Beta" if no_tab else (err or "fetch error")
        age_min = int(cache_age // 60)
        print(f"⚠️ Live fetch failing — {cause} | size=13 color=#c0392b,#ff7b7b")
        print(f"  Last real data: {age_min}m ago · showing {'estimate' if estimated else 'cached value'} | color=#666666,#cfcfcf")
        print("Open claude.ai in Chrome Beta (re-enables live data) | "
              "shell=/bin/bash param1=-lc "
              "param2=\"open -a 'Google Chrome Beta' https://claude.ai/settings/usage\" "
              "terminal=false refresh=true")
        print("---")

    C = "color=#1a1a1a,#ffffff"
    CD = "color=#666666,#cfcfcf"
    OK = "color=#0a7d20,#5dd66d"
    WARN = "color=#b8860b,#e6c200"
    BAD = "color=#c0392b,#ff7b7b"

    print(f"Weekly limit (all models) | size=13 {C}")
    print(f"  {weekly:.0f}% used · resets in {fmt_reset(weekly_reset)} | size=13 {C}")
    if ws_override is not None:
        print(f"  ↻ pace start manually set to {ws_override.strftime('%b %-d %H:%M')} | {CD}")

    if pace:
        eh = pace["elapsed_h"]
        th = pace["total_h"]
        hours_left = pace["hours_left"]
        exp = pace["expected_pct"]
        delta = pace["delta_pp"]
        proj = pace["projected_pct"]
        # Pace verdict: based on projection, fallback to delta
        if proj is not None:
            verdict_color = OK if proj <= 100 else (WARN if proj <= 110 else BAD)
            if proj <= 100:
                verdict = f"on pace — projected {proj:.0f}% by week end"
            else:
                verdict = f"BURNING FAST — projected {proj:.0f}% by week end"
        else:
            verdict_color = CD
            verdict = "warming up — not enough work hours yet"
        print(f"  Used {weekly:.0f}% · expected {exp:.0f}% · Δ {delta:+.0f}pp | {C}")
        print(f"  {verdict} | {verdict_color}")
        print(f"  Worked {eh:.1f}h · {hours_left:.1f}h left (of {th:.1f}h total, {WORK_START_HOUR}:00–{WORK_END_HOUR}:00 daily) | {CD}")

    if sonnet is not None:
        print(f"Weekly Sonnet only | size=13 {C}")
        print(f"  {sonnet:.0f}% used · resets in {fmt_reset(sonnet_reset)} | {CD}")
    if opus_pct is not None:
        print(f"Weekly Opus only | size=13 {C}")
        print(f"  {opus_pct:.0f}% used · resets in {fmt_reset(opus_reset)} | {CD}")

    print("---")
    print(f"Current 5h session | size=13 {C}")
    print(f"  {session:.0f}% used · resets in {fmt_reset(session_reset)} | {CD}")

    if extra:
        used = extra.get("used_credits", 0) / 100.0
        cap = extra.get("monthly_limit", 0) / 100.0
        epct = extra.get("utilization", 0) or 0
        enabled = extra.get("is_enabled")
        cur = extra.get("currency", "USD")
        print("---")
        print(f"Extra usage ({'on' if enabled else 'off'}) | size=13 {C}")
        print(f"  {cur} {used:,.2f} of {cur} {cap:,.0f} ({epct:.1f}%) | {CD}")

    # Active sessions — context-fill snapshot for /compact warnings
    try:
        sys.path.insert(0, str(SCRIPT.parent))
        from _session_lib import scan_sessions
        sessions = scan_sessions(max_age_minutes=60)
    except Exception:
        sessions = []
    if sessions:
        warn = [s for s in sessions if s["flag"] != "ok"]
        print("---")
        title = f"Active sessions: {len(sessions)}"
        if warn:
            title += f" · {len(warn)} need /compact"
        print(f"{title} | size=13 {C}")
        for s in sessions[:6]:
            if s["flag"] == "expensive":
                emoji, color = "🔴", BAD
            elif s["flag"] == "compact_soon":
                emoji, color = "🟠", WARN
            elif s["flag"] == "watch":
                emoji, color = "🟡", WARN
            else:
                emoji, color = "🟢", CD
            mins = s["minutes_since_last_turn"]
            mins_s = f"{mins}m ago" if mins is not None else "—"
            proj = s["project_dir"][1:] if s["project_dir"].startswith("-") else s["project_dir"]
            proj = proj[:28]
            print(f"  {emoji} {s['fill_pct']:>5.1f}%  {mins_s:>7}  {proj} | font=Menlo {color}")

    print("---")
    age = int(time.time() - fetched_at)
    age_s = f"{age}s ago" if age < 60 else f"{age // 60}m ago"
    if estimated:
        print(f"Weekly is ~estimated from local tokens (Chrome unavailable) | {CD}")
    print(f"Updated {age_s}{' — STALE' if stale else ''} | {CD}")
    print("Open claude.ai/settings/usage | href=https://claude.ai/settings/usage")
    print("Refresh now | refresh=true")
    print(f"Set pace start time… | shell={sys.executable} param1={SCRIPT} "
          f"param2=--set-start terminal=false refresh=true")
    if ws_override is not None:
        print(f"Clear start-time override (back to auto) | shell={sys.executable} "
              f"param1={SCRIPT} param2=--clear-start terminal=false refresh=true")
    print(f"Edit plugin | shell=/usr/bin/open param1=-a param2=TextEdit param3={SCRIPT} terminal=false")


if __name__ == "__main__":
    main()
