"""Antigravity weekly/session usage from the agy CLI.

Executes `agy --print "/usage" --output-format json` to retrieve usage buckets
and reset times.

Cached to ~/.cache/antigravity-usage.json.
"""

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

CACHE = Path(os.environ.get("ANTIGRAVITY_USAGE_CACHE")
             or Path.home() / ".cache" / "antigravity-usage.json")
TIMEOUT = 8  # seconds

def _get_agy_path():
    # Primary: user's local bin
    local_agy = Path.home() / ".local" / "bin" / "agy"
    if local_agy.exists():
        return str(local_agy)
    # Fallback to PATH
    return "agy"

def _fetch_usage_json():
    agy = _get_agy_path()
    try:
        proc = subprocess.run(
            [agy, "--print", "/usage", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None

def _parse(data):
    if not isinstance(data, dict):
        return None
    
    # We expect data to have structure:
    # command -> data -> groups -> [buckets]
    cmd = data.get("command") or {}
    cmd_data = cmd.get("data") or {}
    groups = cmd_data.get("groups") or []
    
    normalized = {}
    for g in groups:
        name = g.get("name") or ""
        # Normalize group name to a standard key: "gemini" or "third_party"
        name_lower = name.lower()
        if "gemini" in name_lower:
            key = "gemini"
        elif "claude" in name_lower or "gpt" in name_lower or "3p" in name_lower:
            key = "third_party"
        else:
            key = name_lower.replace(" ", "_")
            
        buckets = g.get("buckets") or []
        group_data = {}
        for b in buckets:
            window = b.get("window") # "weekly" or "5h"
            rem = b.get("remaining_fraction")
            if window and rem is not None:
                # convert remaining fraction to used percentage
                pct = (1.0 - float(rem)) * 100.0
                # clamp to [0, 100] just in case
                pct = max(0.0, min(100.0, pct))
                bucket_key = "weekly" if window == "weekly" else "session"
                group_data[bucket_key] = {
                    "pct": pct,
                    "resets_at": b.get("reset_time") # ISO 8601 string
                }
        if group_data:
            normalized[key] = group_data
            
    return normalized

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
    """Freshest Antigravity usage snapshot, or last cached one.
    Returns:
        {
            "gemini": {
                "weekly": {"pct": float, "resets_at": str},
                "session": {"pct": float, "resets_at": str}
            },
            "third_party": {
                "weekly": {"pct": float, "resets_at": str},
                "session": {"pct": float, "resets_at": str}
            },
            "fetched_at": float,
            "from_cache": bool
        } or None
    """
    raw_data = _fetch_usage_json()
    if raw_data is not None:
        parsed = _parse(raw_data)
        if parsed:
            data = parsed
            data["fetched_at"] = dt.datetime.now().timestamp()
            data["from_cache"] = False
            _write_cache(data)
            return data
            
    cached = _read_cache()
    if cached:
        cached["from_cache"] = True
        return cached
    return None
