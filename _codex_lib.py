"""Codex weekly/session usage from local rollout logs.

Codex CLI writes a `token_count` event each turn into its session rollout
JSONL (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl). Each event carries a
`rate_limits` block whose primary/secondary keys can change meaning between
client versions. Windows are identified by `window_minutes`: 300 for the
current 5h session and 10080 for weekly. Newer clients may provide only the
weekly window. Each window includes used_percent and resets_at (unix epoch
seconds). The percentages are account-wide, so the freshest snapshot (latest
event timestamp across recent files) is the current state — no Chrome/API call
needed.

Cached to ~/.cache/codex-usage.json so the menu still shows last-known usage
when Codex hasn't run recently.

Imported by claude-usage.5m.py (SwiftBar). The leading underscore tells
SwiftBar not to try to execute this file.
"""

import datetime as dt
import glob
import json
import os
from pathlib import Path

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CACHE = Path.home() / ".cache" / "codex-usage.json"
SCAN_DAYS = 9  # only inspect rollout files modified within this window


def _iter_recent_rollouts(scan_days=SCAN_DAYS):
    cutoff = (dt.datetime.now() - dt.timedelta(days=scan_days)).timestamp()
    for path in glob.glob(str(SESSIONS_DIR / "*" / "*" / "*" / "rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        yield path


def _latest_snapshot():
    """Return (timestamp, rate_limits dict) from the newest-timestamped event
    across recent rollout files, or None."""
    best_ts = None
    best_rl = None
    for path in _iter_recent_rollouts():
        try:
            with open(path, "r", errors="ignore") as f:
                for line in f:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    rl = (rec.get("payload") or {}).get("rate_limits")
                    ts = rec.get("timestamp")
                    if not rl or not ts:
                        continue
                    if best_ts is None or ts > best_ts:
                        best_ts, best_rl = ts, rl
        except OSError:
            continue
    if best_rl is None:
        return None
    return best_ts, best_rl


def _window(rl, key):
    w = rl.get(key) or {}
    pct = w.get("used_percent")
    resets = w.get("resets_at")
    if pct is None or resets is None:
        return None
    return {"pct": float(pct), "resets_at": int(resets), "window_minutes": w.get("window_minutes")}


def _window_for_duration(rl, window_minutes):
    """Find a rate-limit window by duration, independent of its API key.

    Codex historically used primary=5h and secondary=7d, but newer clients
    can expose only primary=7d. The duration is the stable discriminator.
    """
    for key in ("primary", "secondary"):
        window = rl.get(key) or {}
        if window.get("window_minutes") == window_minutes:
            return _window(rl, key)
    return None


def _write_cache(data):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data))
    except Exception:
        pass


def _read_cache():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return None


def read_usage():
    """Freshest Codex usage snapshot, or last cached one. Returns a dict with:
        weekly  {pct, resets_at, window_minutes}
        session {pct, resets_at, window_minutes}  (may be None)
        plan_type, snapshot_ts, fetched_at, from_cache
    or None if Codex usage has never been seen."""
    snap = _latest_snapshot()
    if snap is not None:
        ts, rl = snap
        weekly = _window_for_duration(rl, 10080)
        if weekly is not None:
            data = {
                "weekly": weekly,
                "session": _window_for_duration(rl, 300),
                "plan_type": rl.get("plan_type"),
                "snapshot_ts": ts,
                "fetched_at": dt.datetime.now().timestamp(),
                "from_cache": False,
            }
            _write_cache(data)
            return data
    cached = _read_cache()
    if cached:
        cached["from_cache"] = True
        return cached
    return None
