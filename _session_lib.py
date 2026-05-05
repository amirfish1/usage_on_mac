"""Per-session context-fill helpers for Claude Code transcripts.

For any active session, the LAST assistant message in its JSONL contains a
`usage` block with input_tokens + cache_read_input_tokens + cache_creation_input_tokens.
The sum of those three is exactly what was sent to the model on that turn —
i.e. the current context-fill of that session.

Imported by both ccc-context-fill.py (CLI) and claude-usage.5m.py (SwiftBar).
The leading underscore tells SwiftBar not to try to execute this file.
"""

import datetime as dt
import glob
import json
import os
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"


STANDARD_WINDOW = 200_000
LARGE_WINDOW = 1_000_000


def determine_window(model: str, loaded_tokens: int = 0) -> int:
    """Pick context window. JSONL doesn't always tag 1M sessions in the model id,
    so if we observe loaded_tokens > 200K we infer 1M tier (the request would
    otherwise have been rejected)."""
    m = (model or "").lower()
    if "[1m]" in m or "-1m" in m:
        return LARGE_WINDOW
    if loaded_tokens > STANDARD_WINDOW:
        return LARGE_WINDOW
    return STANDARD_WINDOW


# Backward-compat alias
def model_window(model: str) -> int:
    return determine_window(model)


def _last_assistant_turn(path):
    last = None
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
                msg = rec.get("message") or {}
                if not msg.get("usage"):
                    continue
                last = rec
    except Exception:
        pass
    return last


def _classify(fill_pct):
    if fill_pct >= 85:
        return "expensive"      # 🔴 — at risk of auto-compact / expensive turns
    if fill_pct >= 65:
        return "compact_soon"   # 🟠 — should /compact in the next few turns
    if fill_pct >= 45:
        return "watch"          # 🟡 — heads up, growing
    return "ok"                  # 🟢


def session_summary(path):
    """Returns a dict describing the session's last-turn context state, or None."""
    rec = _last_assistant_turn(path)
    if not rec:
        return None
    msg = rec["message"]
    usage = msg.get("usage") or {}
    in_t = usage.get("input_tokens") or 0
    cr = usage.get("cache_read_input_tokens") or 0
    cw = usage.get("cache_creation_input_tokens") or 0
    loaded = in_t + cr + cw
    model = msg.get("model") or rec.get("model") or ""
    window = determine_window(model, loaded)
    fill_pct = round(loaded / window * 100, 1) if window else 0.0
    ts = rec.get("timestamp")
    minutes_since = None
    if ts:
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            minutes_since = int((dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 60)
        except Exception:
            pass
    return {
        "session_id": Path(path).stem,
        "project_dir": Path(path).parent.name,
        "jsonl_path": str(path),
        "model": model,
        "context_window": window,
        "context_loaded": loaded,
        "fill_pct": fill_pct,
        "last_turn_at": ts,
        "minutes_since_last_turn": minutes_since,
        "flag": _classify(fill_pct),
    }


def scan_sessions(max_age_minutes=60):
    """Return list of session summaries for transcripts modified within max_age_minutes.
    Sorted by fill_pct descending."""
    cutoff = (dt.datetime.now() - dt.timedelta(minutes=max_age_minutes)).timestamp()
    out = []
    for path in glob.glob(str(PROJECTS_DIR / "*" / "*.jsonl")):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        s = session_summary(path)
        if s:
            out.append(s)
    out.sort(key=lambda s: s["fill_pct"], reverse=True)
    return out
