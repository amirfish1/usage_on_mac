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
import urllib.error
import urllib.request
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
CCC_USAGE_URL = os.environ.get("CCC_USAGE_URL", "http://127.0.0.1:8090/api/usage/current")

try:
    sys.path.insert(0, str(SCRIPT.parent))
    from _icons_lib import (
        get_dropdown_icon_base64,
        render_menubar_image,
        is_dark_mode,
        HAS_PIL,
    )
    HAS_ICONS = HAS_PIL
except Exception:
    HAS_ICONS = False
    get_dropdown_icon_base64 = lambda name, theme=None: ""
    render_menubar_image = lambda segments, prefix="", suffix="", theme=None: None

# xbar renders any dropdown line with no href=/shell=/refresh= action as a
# disabled NSMenuItem, which macOS always draws dimmed regardless of a
# custom color — a harmless no-op shell action is the only way to get
# purely-informational rows to render at full brightness.
NOOP = "shell=/usr/bin/true terminal=false"

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


def fetch_from_ccc():
    try:
        req = urllib.request.Request(CCC_USAGE_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as res:
            data = json.loads(res.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    claude = data.get("claude") or {}
    weekly = claude.get("seven_day") or {}
    session = claude.get("five_hour") or {}
    pace = claude.get("pace") or {}
    if (
        weekly.get("pct") is None
        or not weekly.get("resets_at")
        or session.get("pct") is None
        or not session.get("resets_at")
        or not pace.get("ok")
    ):
        return None
    fetched_at = data.get("fetched_at")
    try:
        fetched_dt = parse_iso(fetched_at)
        if (datetime.now(timezone.utc) - fetched_dt.astimezone(timezone.utc)).total_seconds() > STALE_AFTER:
            return None
    except Exception:
        return None
    return data


def usage_from_ccc(data):
    """Normalize CCC's Claude fields to the direct Anthropic response shape."""
    claude = data.get("claude") or {}

    def window(name):
        source = claude.get(name) or {}
        return {
            "utilization": source.get("pct"),
            "resets_at": source.get("resets_at"),
        }

    return {
        "five_hour": window("five_hour"),
        "seven_day": window("seven_day"),
        "seven_day_sonnet": window("seven_day_sonnet"),
        "seven_day_opus": window("seven_day_opus"),
        "seven_day_fable": window("seven_day_fable"),
        "extra_usage": claude.get("extra_usage") or {},
    }


def scoped_weekly_from_limits(usage):
    """Per-model weekly buckets hiding in the payload's `limits` array.

    The settings/usage API often has null seven_day_<model> fields while the
    same number rides along as a weekly_scoped limit with a model scope
    (observed for Fable). Returns {family_lower: {utilization, resets_at}}."""
    out = {}
    for lim in usage.get("limits") or []:
        if lim.get("kind") != "weekly_scoped":
            continue
        model = (lim.get("scope") or {}).get("model") or {}
        name = (model.get("display_name") or "").strip().lower()
        if name and lim.get("percent") is not None:
            out[name] = {
                "utilization": lim.get("percent"),
                "resets_at": lim.get("resets_at"),
            }
    return out


def burn_shares_for_week(weekly_reset_iso):
    """{family: fraction-of-burn} from local transcripts over the API week
    (reset minus 7 days — deliberately ignores the manual pace override so the
    split always matches Anthropic's counting window). None on any failure."""
    if not weekly_reset_iso:
        return None
    try:
        week_start = parse_iso(weekly_reset_iso).astimezone() - timedelta(days=7)
        sys.path.insert(0, str(SCRIPT.parent))
        from usage_by_model import usage_by_model
        fams = usage_by_model(week_start)
    except Exception:
        return None
    if not (fams.get("_total") or {}).get("burn"):
        return None
    return {k: v["share"] for k, v in fams.items() if k != "_total"}


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


def fmt_reset_epoch(epoch):
    """fmt_reset for a unix-epoch reset time (Codex uses epochs, not ISO)."""
    if not epoch:
        return ""
    try:
        if isinstance(epoch, str):
            return fmt_reset(epoch)
        return fmt_reset(datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat())
    except Exception:
        return ""


def days_until(resets_at):
    """Whole days from now until an ISO or unix-epoch reset timestamp, or None."""
    if not resets_at:
        return None
    try:
        if isinstance(resets_at, str):
            t = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        else:
            t = datetime.fromtimestamp(resets_at, tz=timezone.utc)
        secs = (t - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(secs // 86400))
    except Exception:
        return None


def bar_segment_text(pct, pace, resets_at=None):
    """One menu-bar provider segment value: 'weekly%·proj%' (proj omitted if N/A).

    Once a limit is hit (pct >= 100), a projection is meaningless — swap it
    for days left until the limit resets instead.
    """
    if pct is None:
        return "—"
    if pct >= 100:
        d = days_until(resets_at)
        if d is not None:
            return f"{pct:.0f}%·{d}d"
        return f"{pct:.0f}%"
    if pace and pace["projected_pct"] is not None:
        return f"{pct:.0f}%·{pace['projected_pct']:.0f}"
    return f"{pct:.0f}%"


def bar_segment(icon, pct, pace, resets_at=None):
    """One menu-bar provider segment: 'icon weekly%·proj%' (proj omitted if N/A)."""
    return f"{icon} {bar_segment_text(pct, pace, resets_at)}"


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


def compute_pace(weekly_pct, week_start_local, resets_local):
    """Core pace math, provider-agnostic. Given a weekly %, the week-start and
    the reset instant (both local datetimes), return the pace dict, or None."""
    if weekly_pct is None or week_start_local is None or resets_local is None:
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


def pace_for(weekly_pct, weekly_resets_at_iso):
    """Claude pace: week-start comes from get_week_start (honors the manual override)."""
    if weekly_pct is None or not weekly_resets_at_iso:
        return None
    week_start_local = get_week_start(weekly_resets_at_iso)
    if not week_start_local:
        return None
    try:
        resets_local = parse_iso(weekly_resets_at_iso).astimezone()
    except Exception:
        return None
    return compute_pace(weekly_pct, week_start_local, resets_local)


def codex_pace(weekly_pct, weekly_resets_epoch, window_minutes):
    """Codex pace: week-start is the reset instant minus the rate-limit window
    (no manual override — Codex's % is always live from local logs)."""
    if weekly_pct is None or not weekly_resets_epoch:
        return None
    try:
        if isinstance(weekly_resets_epoch, str):
            resets_local = parse_iso(weekly_resets_epoch).astimezone()
        else:
            resets_local = datetime.fromtimestamp(weekly_resets_epoch, tz=timezone.utc).astimezone()
    except Exception:
        return None
    week_start_local = resets_local - timedelta(minutes=window_minutes or 10080)
    return compute_pace(weekly_pct, week_start_local, resets_local)


def pace_verdict(proj, OK, WARN, BAD):
    """(verdict_text, color) for a projection %. Shared by Claude and Codex.

    color is "" (no attribute — plain, full-contrast default text) when
    there's no real status to convey, so neutral rows never get forced into
    xbar's dimmer custom-color rendering."""
    if proj is not None:
        color = OK if proj <= 100 else (WARN if proj <= 110 else BAD)
        if proj <= 100:
            return f"on pace — projected {proj:.0f}% by week end", color
        return f"BURNING FAST — projected {proj:.0f}% by week end", color
    return "warming up — not enough work hours yet", ""


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


def format_proj_dir(proj_dir: str, max_len: int = 28) -> str:
    """Format project directory names cleanly for menu dropdown."""
    if not proj_dir:
        return ""
    s = proj_dir.lstrip("-")
    home = Path.home()
    home_prefix = str(home).strip("/").replace("/", "-")
    if s.startswith(home_prefix + "-"):
        s = "~/" + s[len(home_prefix) + 1:]
    elif s.startswith("Users-"):
        parts = s.split("-", 2)
        if len(parts) == 3:
            s = "~/" + parts[2]
    if len(s) > max_len:
        s = "..." + s[-(max_len - 3):]
    return s


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--set-start":
            cli_set_start()
            return
        if sys.argv[1] == "--clear-start":
            cli_clear_start()
            return

    ccc_usage = fetch_from_ccc()
    if ccc_usage:
        fresh, err = usage_from_ccc(ccc_usage), None
    else:
        fresh, err = fetch_via_chrome()
    estimated = False

    if fresh is not None:
        if not ccc_usage:
            write_cache(fresh)
        usage = fresh
        fetched_at = (
            parse_iso(ccc_usage["fetched_at"]).timestamp()
            if ccc_usage else time.time()
        )
        stale = False
        # Calibrate: pair (this week's local tokens) with (real weekly %)
        if not ccc_usage:
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
            print(f"Can't fetch usage: {err} | color=red {NOOP}")
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
    ws_override = None if ccc_usage else active_weekstart_override(weekly_reset)
    session = (usage.get("five_hour") or {}).get("utilization")
    session_reset = (usage.get("five_hour") or {}).get("resets_at")
    # Per-model weekly buckets: explicit seven_day_<family> fields win, the
    # limits array fills the gaps (Fable currently only appears there).
    model_weekly = scoped_weekly_from_limits(usage)
    for fam in ("sonnet", "opus", "fable"):
        bucket = usage.get(f"seven_day_{fam}") or {}
        if bucket.get("utilization") is not None:
            model_weekly[fam] = {
                "utilization": bucket["utilization"],
                "resets_at": bucket.get("resets_at"),
            }
    extra = usage.get("extra_usage") or {}

    if ccc_usage:
        pace = (ccc_usage.get("claude") or {}).get("pace") or None
    else:
        pace = pace_for(weekly, weekly_reset)

    # Prefer a fresh local Codex snapshot over CCC. CCC can remain fresh for
    # Claude while its independently sourced Codex snapshot has gone stale.
    local_codex = None
    try:
        sys.path.insert(0, str(SCRIPT.parent))
        from _codex_lib import read_usage as codex_read_usage
        local_codex = codex_read_usage()
    except Exception:
        local_codex = None
    ccc_codex = (ccc_usage.get("codex") or None) if ccc_usage else None
    if local_codex and not local_codex.get("from_cache"):
        codex = local_codex
    else:
        codex = ccc_codex or local_codex
    codex_weekly = codex_weekly_reset = codex_pace_d = None
    codex_session = codex_session_reset = None
    if codex:
        cw = codex.get("weekly") or {}
        codex_weekly = cw.get("pct")
        codex_weekly_reset = cw.get("resets_at")
        codex_pace_d = codex_pace(
            codex_weekly,
            codex_weekly_reset,
            cw.get("window_minutes"),
        )
        cs = codex.get("session") or {}
        codex_session = cs.get("pct")
        codex_session_reset = cs.get("resets_at")

    # Kimi (Kimi Code CLI) usage via its local OAuth token + usages API.
    kimi = None
    try:
        sys.path.insert(0, str(SCRIPT.parent))
        from _kimi_lib import read_usage as kimi_read_usage
        kimi = kimi_read_usage()
    except Exception:
        kimi = None
    kimi_weekly = kimi_weekly_reset = kimi_pace_d = None
    kimi_session = kimi_session_reset = None
    kimi_extra = None
    if kimi:
        kw = kimi.get("weekly") or {}
        kimi_weekly = kw.get("pct")
        kimi_weekly_reset = kw.get("resets_at")
        kimi_pace_d = codex_pace(kimi_weekly, kimi_weekly_reset, 10080)
        ks = kimi.get("session") or {}
        kimi_session = ks.get("pct")
        kimi_session_reset = ks.get("resets_at")
        kimi_extra = kimi.get("extra")

    # Antigravity CLI usage via its local JSON/CLI.
    local_antigravity = None
    try:
        sys.path.insert(0, str(SCRIPT.parent))
        from _antigravity_lib import read_usage as antigravity_read_usage
        local_antigravity = antigravity_read_usage()
    except Exception:
        local_antigravity = None
    agy_gemini_weekly = agy_gemini_weekly_reset = agy_gemini_pace = None
    agy_gemini_session = agy_gemini_session_reset = None
    agy_3p_weekly = agy_3p_weekly_reset = agy_3p_pace = None
    agy_3p_session = agy_3p_session_reset = None
    if local_antigravity:
        ag_gem = local_antigravity.get("gemini") or {}
        if ag_gem:
            ag_gw = ag_gem.get("weekly") or {}
            agy_gemini_weekly = ag_gw.get("pct")
            agy_gemini_weekly_reset = ag_gw.get("resets_at")
            agy_gemini_pace = codex_pace(agy_gemini_weekly, agy_gemini_weekly_reset, 10080)
            ag_gs = ag_gem.get("session") or {}
            agy_gemini_session = ag_gs.get("pct")
            agy_gemini_session_reset = ag_gs.get("resets_at")
        ag_3p = local_antigravity.get("third_party") or {}
        if ag_3p:
            ag_3pw = ag_3p.get("weekly") or {}
            agy_3p_weekly = ag_3pw.get("pct")
            agy_3p_weekly_reset = ag_3pw.get("resets_at")
            agy_3p_pace = codex_pace(agy_3p_weekly, agy_3p_weekly_reset, 10080)
            ag_3ps = ag_3p.get("session") or {}
            agy_3p_session = ag_3ps.get("pct")
            agy_3p_session_reset = ag_3ps.get("resets_at")

    # Grok (xAI) usage via local CLI logs.
    local_grok = None
    try:
        sys.path.insert(0, str(SCRIPT.parent))
        from _grok_lib import read_usage as grok_read_usage
        local_grok = grok_read_usage()
    except Exception:
        local_grok = None
    grok_weekly = grok_weekly_reset = grok_pace_d = None
    grok_extra = None
    if local_grok:
        gw = local_grok.get("weekly") or {}
        grok_weekly = gw.get("pct")
        grok_weekly_reset = gw.get("resets_at")
        grok_pace_d = codex_pace(
            grok_weekly,
            grok_weekly_reset,
            gw.get("window_minutes") or 10080,
        )
        grok_extra = local_grok.get("extra")

    # Was the live fetch broken on this run?
    fetch_failed = (fresh is None)
    no_tab = bool(err and "no claude.ai tab" in (err or "").lower())
    cache_age = time.time() - fetched_at

    # Build provider segments with real brand symbols and hybrid numbers
    segments = []
    c_weekly_text = f"{weekly:.0f}%" if weekly is not None else "—"
    if estimated:
        c_weekly_text += "~est"
    c_pace_text = (
        f"{days_until(weekly_reset)}d" if (weekly is not None and weekly >= 100 and days_until(weekly_reset) is not None)
        else (f"{pace['projected_pct']:.0f}" if (pace and pace["projected_pct"] is not None) else None)
    )
    segments.append({
        "icon": "claude",
        "symbol": "✳",
        "name": "Claude",
        "weekly_text": c_weekly_text,
        "pace_text": c_pace_text,
        "weekly_pct": weekly,
        "pace_pct": pace.get("projected_pct") if pace else None,
        "text": bar_segment_text(weekly, pace, weekly_reset) + ("~est" if estimated else ""),
    })

    if codex_weekly is not None:
        cx_pace_text = (
            f"{days_until(codex_weekly_reset)}d" if (codex_weekly >= 100 and days_until(codex_weekly_reset) is not None)
            else (f"{codex_pace_d['projected_pct']:.0f}" if (codex_pace_d and codex_pace_d["projected_pct"] is not None) else None)
        )
        segments.append({
            "icon": "codex",
            "symbol": "⬡",
            "name": "Codex",
            "weekly_text": f"{codex_weekly:.0f}%",
            "pace_text": cx_pace_text,
            "weekly_pct": codex_weekly,
            "pace_pct": codex_pace_d.get("projected_pct") if codex_pace_d else None,
            "text": bar_segment_text(codex_weekly, codex_pace_d, codex_weekly_reset),
        })
    if kimi_weekly is not None:
        k_pace_text = (
            f"{days_until(kimi_weekly_reset)}d" if (kimi_weekly >= 100 and days_until(kimi_weekly_reset) is not None)
            else (f"{kimi_pace_d['projected_pct']:.0f}" if (kimi_pace_d and kimi_pace_d["projected_pct"] is not None) else None)
        )
        segments.append({
            "icon": "kimi",
            "symbol": "🌙",
            "name": "Kimi",
            "weekly_text": f"{kimi_weekly:.0f}%",
            "pace_text": k_pace_text,
            "weekly_pct": kimi_weekly,
            "pace_pct": kimi_pace_d.get("projected_pct") if kimi_pace_d else None,
            "text": bar_segment_text(kimi_weekly, kimi_pace_d, kimi_weekly_reset),
        })
    if agy_gemini_weekly is not None:
        ag_pace_text = (
            f"{days_until(agy_gemini_weekly_reset)}d" if (agy_gemini_weekly >= 100 and days_until(agy_gemini_weekly_reset) is not None)
            else (f"{agy_gemini_pace['projected_pct']:.0f}" if (agy_gemini_pace and agy_gemini_pace["projected_pct"] is not None) else None)
        )
        segments.append({
            "icon": "gemini",
            "symbol": "✦",
            "name": "Antigravity",
            "weekly_text": f"{agy_gemini_weekly:.0f}%",
            "pace_text": ag_pace_text,
            "weekly_pct": agy_gemini_weekly,
            "pace_pct": agy_gemini_pace.get("projected_pct") if agy_gemini_pace else None,
            "text": bar_segment_text(agy_gemini_weekly, agy_gemini_pace, agy_gemini_weekly_reset),
        })
    if agy_3p_weekly is not None:
        ag3p_pace_text = (
            f"{days_until(agy_3p_weekly_reset)}d" if (agy_3p_weekly >= 100 and days_until(agy_3p_weekly_reset) is not None)
            else (f"{agy_3p_pace['projected_pct']:.0f}" if (agy_3p_pace and agy_3p_pace["projected_pct"] is not None) else None)
        )
        segments.append({
            "icon": "gemini",
            "symbol": "✦3P",
            "name": "3P",
            "label": "3P",
            "weekly_text": f"{agy_3p_weekly:.0f}%",
            "pace_text": ag3p_pace_text,
            "weekly_pct": agy_3p_weekly,
            "pace_pct": agy_3p_pace.get("projected_pct") if agy_3p_pace else None,
            "text": bar_segment_text(agy_3p_weekly, agy_3p_pace, agy_3p_weekly_reset),
        })
    if grok_weekly is not None:
        gr_pace_text = (
            f"{days_until(grok_weekly_reset)}d" if (grok_weekly >= 100 and days_until(grok_weekly_reset) is not None)
            else (f"{grok_pace_d['projected_pct']:.0f}" if (grok_pace_d and grok_pace_d["projected_pct"] is not None) else None)
        )
        segments.append({
            "icon": "grok",
            "symbol": "𝕏",
            "name": "Grok",
            "weekly_text": f"{grok_weekly:.0f}%",
            "pace_text": gr_pace_text,
            "weekly_pct": grok_weekly,
            "pace_pct": grok_pace_d.get("projected_pct") if grok_pace_d else None,
            "text": bar_segment_text(grok_weekly, grok_pace_d, grok_weekly_reset),
        })

    # Menu bar headline: text with authentic brand symbols (used in CLI / tests)
    bar_parts = [f"{s.get('symbol') or s.get('label') or s['name']} {s['text']}" for s in segments]
    bar = " ".join(bar_parts)
    if stale:
        bar += " (stale)"
    if fetch_failed and cache_age > 3600:
        bar = "⚠️ " + bar
    elif fetch_failed:
        bar = "⚠ " + bar

    # Under xbar: render sharp Retina image with real brand icons and hybrid-colored numbers
    img_b64 = None
    is_test_run = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or "pytest" in sys.modules
        or "unittest" in sys.modules
        or os.environ.get("NO_MENUBAR_IMAGE")
    )
    if HAS_ICONS and not sys.stdout.isatty() and not is_test_run:
        prefix = "⚠️ " if (fetch_failed and cache_age > 3600) else ("⚠ " if fetch_failed else "")
        suffix = " (stale)" if stale else ""
        img_b64 = render_menubar_image(segments, prefix=prefix, suffix=suffix)

    if img_b64:
        print(f" | image={img_b64}")
    else:
        print(f"{bar} | size=12")
    print("---")

    # If live fetch is broken, surface the cause + a one-click fix at the top of the dropdown
    if fetch_failed:
        cause = "no claude.ai tab in Chrome Beta" if no_tab else (err or "fetch error")
        age_min = int(cache_age // 60)
        print(f"⚠️ Live fetch failing — {cause} | size=13 color=#c0392b,#ff7b7b")
        print(f"--  Last real data: {age_min}m ago · showing {'estimate' if estimated else 'cached value'} | {NOOP}")
        print("Open claude.ai in Chrome Beta (re-enables live data) | "
              "shell=/bin/bash param1=-lc "
              "param2=\"open -a 'Google Chrome Beta' https://claude.ai/settings/usage\" "
              "terminal=false refresh=true")
        print("---")

    OK = "color=#0a7d20,#5dd66d"
    WARN = "color=#b8860b,#e6c200"
    BAD = "color=#c0392b,#ff7b7b"

    # Base64 dropdown icon attributes (real 32x32 Retina PNGs)
    icon_claude = f"image={get_dropdown_icon_base64('claude')} " if HAS_ICONS else ""
    icon_codex = f"image={get_dropdown_icon_base64('codex')} " if HAS_ICONS else ""
    icon_kimi = f"image={get_dropdown_icon_base64('kimi')} " if HAS_ICONS else ""
    icon_antigravity = f"image={get_dropdown_icon_base64('antigravity')} " if HAS_ICONS else ""
    icon_gemini = f"image={get_dropdown_icon_base64('gemini')} " if HAS_ICONS else ""
    icon_grok = f"image={get_dropdown_icon_base64('grok')} " if HAS_ICONS else ""

    # Claude Section
    c_status_color = BAD if (weekly is not None and weekly >= 100) else (pace_verdict(pace["projected_pct"], OK, WARN, BAD)[1] if pace else "")
    print(f"Claude (Weekly limit all models) | {icon_claude}size=13")
    print(f"--  {weekly:.0f}% used · resets in {fmt_reset(weekly_reset)}" + (f" | size=13 {c_status_color} {NOOP}" if c_status_color else f" | size=13 {NOOP}"))
    if ws_override is not None:
        print(f"--  ↻ pace start manually set to {ws_override.strftime('%b %-d %H:%M')} | {NOOP}")

    if pace:
        eh = pace["elapsed_h"]
        th = pace["total_h"]
        hours_left = pace["hours_left"]
        exp = pace["expected_pct"]
        delta = pace["delta_pp"]
        proj = pace["projected_pct"]
        verdict, verdict_color = pace_verdict(proj, OK, WARN, BAD)
        print(f"--  {verdict}" + (f" | {verdict_color} {NOOP}" if verdict_color else f" | {NOOP}"))
        print(f"--  expected {exp:.0f}% · Δ {delta:+.0f}pp · Worked {eh:.1f}h · {hours_left:.1f}h left | {NOOP}")

    if session is not None:
        print(f"--  5h session: {session:.0f}% used · resets in {fmt_reset(session_reset)} | {NOOP}")

    if extra:
        used = extra.get("used_credits", 0) / 100.0
        cap = extra.get("monthly_limit", 0) / 100.0
        epct = extra.get("utilization", 0) or 0
        enabled = extra.get("is_enabled")
        cur = extra.get("currency", "USD")
        print(f"--  Extra usage ({'on' if enabled else 'off'}): {cur} {used:,.2f} of {cur} {cap:,.0f} ({epct:.1f}%) | {NOOP}")

    # Model split — nested inside Claude, since it's a Claude-only breakdown.
    shares = burn_shares_for_week(weekly_reset)
    if model_weekly or shares:
        print(f"--  Model split — this week | size=13")
        fams = sorted(
            set(model_weekly) | set(shares or {}),
            key=lambda f: -(shares or {}).get(f, 0.0),
        )
        for fam in fams:
            cap = (model_weekly.get(fam) or {}).get("utilization")
            cap_s = f"{cap:.0f}% of its cap" if cap is not None else "cap —"
            share = (shares or {}).get(fam)
            share_s = f"{share * 100:4.0f}% of burn" if share is not None else "burn —"
            print(f"----  {fam.capitalize():<7} {share_s} · {cap_s} | font=Menlo {NOOP}")

    print(f"--  Open Claude Web UI | href=https://claude.ai/settings/usage")

    # Codex Section
    if codex_weekly is not None:
        print("---")
        plan = codex.get("plan_type")
        cx_status_color = BAD if (codex_weekly is not None and codex_weekly >= 100) else (pace_verdict(codex_pace_d["projected_pct"], OK, WARN, BAD)[1] if codex_pace_d else "")
        print(f"Codex{f' ({plan})' if plan else ''} | {icon_codex}size=13")
        print(f"--  {codex_weekly:.0f}% used · resets in {fmt_reset_epoch(codex_weekly_reset)}" + (f" | size=13 {cx_status_color} {NOOP}" if cx_status_color else f" | size=13 {NOOP}"))
        if codex_pace_d:
            eh = codex_pace_d["elapsed_h"]
            th = codex_pace_d["total_h"]
            hours_left = codex_pace_d["hours_left"]
            exp = codex_pace_d["expected_pct"]
            delta = codex_pace_d["delta_pp"]
            proj = codex_pace_d["projected_pct"]
            verdict, verdict_color = pace_verdict(proj, OK, WARN, BAD)
            print(f"--  {verdict}" + (f" | {verdict_color} {NOOP}" if verdict_color else f" | {NOOP}"))
            print(f"--  expected {exp:.0f}% · Δ {delta:+.0f}pp · Worked {eh:.1f}h · {hours_left:.1f}h left | {NOOP}")
        if codex_session is not None:
            print(f"--  5h session: {codex_session:.0f}% used · resets in {fmt_reset_epoch(codex_session_reset)} | {NOOP}")
        if codex.get("from_cache"):
            print(f"--  (showing last Codex snapshot — no recent activity) | {NOOP}")
        print(f"--  Open Codex Web UI | href=https://chatgpt.com/codex/cloud/settings/analytics#usage")

    # Kimi Section
    if kimi_weekly is not None:
        print("---")
        plan = kimi.get("plan_type")
        k_status_color = BAD if (kimi_weekly is not None and kimi_weekly >= 100) else (pace_verdict(kimi_pace_d["projected_pct"], OK, WARN, BAD)[1] if kimi_pace_d else "")
        print(f"Kimi{f' ({plan})' if plan else ''} | {icon_kimi}size=13")
        print(f"--  {kimi_weekly:.0f}% used · resets in {fmt_reset_epoch(kimi_weekly_reset)}" + (f" | size=13 {k_status_color} {NOOP}" if k_status_color else f" | size=13 {NOOP}"))
        if kimi_pace_d:
            eh = kimi_pace_d["elapsed_h"]
            th = kimi_pace_d["total_h"]
            hours_left = kimi_pace_d["hours_left"]
            exp = kimi_pace_d["expected_pct"]
            delta = kimi_pace_d["delta_pp"]
            proj = kimi_pace_d["projected_pct"]
            verdict, verdict_color = pace_verdict(proj, OK, WARN, BAD)
            print(f"--  {verdict}" + (f" | {verdict_color} {NOOP}" if verdict_color else f" | {NOOP}"))
            print(f"--  expected {exp:.0f}% · Δ {delta:+.0f}pp · Worked {eh:.1f}h · {hours_left:.1f}h left | {NOOP}")
        if kimi_session is not None:
            print(f"--  5h session: {kimi_session:.0f}% used · resets in {fmt_reset_epoch(kimi_session_reset)} | {NOOP}")
        if kimi_extra:
            cur = kimi_extra.get("currency", "USD")
            if kimi_extra.get("monthly_limit_enabled") and kimi_extra.get("monthly_limit_cents"):
                used = kimi_extra.get("monthly_used_cents", 0) / 100.0
                cap = kimi_extra["monthly_limit_cents"] / 100.0
                epct = (used / cap * 100) if cap else 0
                print(f"--  Extra usage (on): {cur} {used:,.2f} of {cur} {cap:,.0f} ({epct:.1f}%) | {NOOP}")
            else:
                bal = kimi_extra.get("balance_cents")
                if bal is None:
                    bal = kimi_extra.get("total_cents", 0)
                print(f"--  Extra usage balance: {cur} {bal / 100.0:,.2f} | {NOOP}")
        if kimi.get("from_cache"):
            print(f"--  (showing last Kimi snapshot — fetch failed) | {NOOP}")
        print(f"--  Open Kimi Web UI | href=https://www.kimi.com/membership/subscription?tab=quota")

    # Antigravity Section
    if agy_gemini_weekly is not None or agy_3p_weekly is not None:
        print("---")
        print(f"Antigravity | {icon_antigravity}size=13")
        if agy_gemini_weekly is not None:
            ag_status_color = BAD if (agy_gemini_weekly is not None and agy_gemini_weekly >= 100) else (pace_verdict(agy_gemini_pace["projected_pct"], OK, WARN, BAD)[1] if agy_gemini_pace else "")
            print(f"--  Gemini Models | size=12 {NOOP}")
            print(f"----  {agy_gemini_weekly:.0f}% used · resets in {fmt_reset(agy_gemini_weekly_reset)}" + (f" | size=12 {ag_status_color} {NOOP}" if ag_status_color else f" | size=12 {NOOP}"))
            if agy_gemini_pace:
                eh = agy_gemini_pace["elapsed_h"]
                th = agy_gemini_pace["total_h"]
                hours_left = agy_gemini_pace["hours_left"]
                exp = agy_gemini_pace["expected_pct"]
                delta = agy_gemini_pace["delta_pp"]
                proj = agy_gemini_pace["projected_pct"]
                verdict, verdict_color = pace_verdict(proj, OK, WARN, BAD)
                print(f"----  {verdict}" + (f" | {verdict_color} {NOOP}" if verdict_color else f" | {NOOP}"))
                print(f"----  expected {exp:.0f}% · Δ {delta:+.0f}pp · Worked {eh:.1f}h · {hours_left:.1f}h left | {NOOP}")
            if agy_gemini_session is not None:
                print(f"----  5h session: {agy_gemini_session:.0f}% used · resets in {fmt_reset(agy_gemini_session_reset)} | {NOOP}")
        if agy_3p_weekly is not None:
            ag3p_status_color = BAD if (agy_3p_weekly is not None and agy_3p_weekly >= 100) else (pace_verdict(agy_3p_pace["projected_pct"], OK, WARN, BAD)[1] if agy_3p_pace else "")
            if agy_gemini_weekly is not None:
                print(f"--  | {NOOP}")
            print(f"--  Claude/GPT Models | size=12 {NOOP}")
            print(f"----  {agy_3p_weekly:.0f}% used · resets in {fmt_reset(agy_3p_weekly_reset)}" + (f" | size=12 {ag3p_status_color} {NOOP}" if ag3p_status_color else f" | size=12 {NOOP}"))
            if agy_3p_pace:
                eh = agy_3p_pace["elapsed_h"]
                th = agy_3p_pace["total_h"]
                hours_left = agy_3p_pace["hours_left"]
                exp = agy_3p_pace["expected_pct"]
                delta = agy_3p_pace["delta_pp"]
                proj = agy_3p_pace["projected_pct"]
                verdict, verdict_color = pace_verdict(proj, OK, WARN, BAD)
                print(f"----  {verdict}" + (f" | {verdict_color} {NOOP}" if verdict_color else f" | {NOOP}"))
                print(f"----  expected {exp:.0f}% · Δ {delta:+.0f}pp · Worked {eh:.1f}h · {hours_left:.1f}h left | {NOOP}")
            if agy_3p_session is not None:
                print(f"----  5h session: {agy_3p_session:.0f}% used · resets in {fmt_reset(agy_3p_session_reset)} | {NOOP}")
        if local_antigravity and local_antigravity.get("from_cache"):
            print(f"--  (showing last Antigravity snapshot — fetch failed) | {NOOP}")
        print(f"--  Open Antigravity Home | href=https://antigravity.google/docs")

    # Grok Section
    if grok_weekly is not None:
        print("---")
        plan = local_grok.get("plan_type")
        gr_status_color = BAD if (grok_weekly is not None and grok_weekly >= 100) else (pace_verdict(grok_pace_d["projected_pct"], OK, WARN, BAD)[1] if grok_pace_d else "")
        print(f"Grok{f' ({plan})' if plan else ''} | {icon_grok}size=13")
        print(f"--  {grok_weekly:.0f}% used · resets in {fmt_reset(grok_weekly_reset)}" + (f" | size=13 {gr_status_color} {NOOP}" if gr_status_color else f" | size=13 {NOOP}"))
        if grok_pace_d:
            eh = grok_pace_d["elapsed_h"]
            th = grok_pace_d["total_h"]
            hours_left = grok_pace_d["hours_left"]
            exp = grok_pace_d["expected_pct"]
            delta = grok_pace_d["delta_pp"]
            proj = grok_pace_d["projected_pct"]
            verdict, verdict_color = pace_verdict(proj, OK, WARN, BAD)
            print(f"--  {verdict}" + (f" | {verdict_color} {NOOP}" if verdict_color else f" | {NOOP}"))
            print(f"--  expected {exp:.0f}% · Δ {delta:+.0f}pp · Worked {eh:.1f}h · {hours_left:.1f}h left | {NOOP}")
        if grok_extra:
            bal = grok_extra.get("prepaid_balance_cents", 0)
            if bal:
                print(f"--  Extra usage balance: USD {bal / 100.0:,.2f} | {NOOP}")
        if local_grok.get("from_cache"):
            print(f"--  (showing last Grok snapshot — from cache) | {NOOP}")
        print(f"--  Open Grok Web UI | href=https://grok.com")

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
        print(f"{title} | size=13")
        for s in sessions[:6]:
            if s["flag"] == "expensive":
                emoji, color = "🔴", BAD
            elif s["flag"] == "compact_soon":
                emoji, color = "🟠", WARN
            elif s["flag"] == "watch":
                emoji, color = "🟡", WARN
            else:
                emoji, color = "🟢", ""
            mins = s["minutes_since_last_turn"]
            mins_s = f"{mins}m ago" if mins is not None else "—"
            proj = format_proj_dir(s["project_dir"])
            attr = f" {color}" if color else ""
            print(f"--  {emoji} {s['fill_pct']:>5.1f}%  {mins_s:>7}  {proj} | font=Menlo{attr} {NOOP}")

    print("---")
    print(f"🌐 Open Web UIs | size=13")
    print(f"--  Claude (claude.ai) | {icon_claude}href=https://claude.ai/settings/usage")
    print(f"--  Codex (chatgpt.com) | {icon_codex}href=https://chatgpt.com/codex/cloud/settings/analytics#usage")
    print(f"--  Kimi (kimi.com) | {icon_kimi}href=https://www.kimi.com/membership/subscription?tab=quota")
    print(f"--  Antigravity (antigravity.google) | {icon_antigravity}href=https://antigravity.google/docs")
    if grok_weekly is not None or os.path.exists(os.path.expanduser("~/.grok")):
        print(f"--  Grok (grok.com) | {icon_grok}href=https://grok.com")
    print("---")
    age = int(time.time() - fetched_at)
    age_s = f"{age}s ago" if age < 60 else f"{age // 60}m ago"
    if estimated:
        print(f"Weekly is ~estimated from local tokens (Chrome unavailable) | {NOOP}")
    print(f"Updated {age_s}{' — STALE' if stale else ''} | {NOOP}")
    print("Refresh now | refresh=true")
    print(f"Set pace start time… | shell={sys.executable} param1={SCRIPT} "
          f"param2=--set-start terminal=false refresh=true")
    if ws_override is not None:
        print(f"Clear start-time override (back to auto) | shell={sys.executable} "
              f"param1={SCRIPT} param2=--clear-start terminal=false refresh=true")
    print(f"Edit plugin | shell=/usr/bin/open param1=-a param2=TextEdit param3={SCRIPT} terminal=false")


if __name__ == "__main__":
    main()

