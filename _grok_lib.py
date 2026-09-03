"""Grok (xAI) weekly usage from local CLI logs.

Grok CLI logs its billing configuration into `~/.grok/logs/unified.jsonl` on startup
and credit fetch events. Each event contains:
  - creditUsagePercent: weekly % used
  - currentPeriod: start and end ISO timestamps (weekly reset)
  - subscriptionTier: plan name (e.g. "SuperGrok")
  - prepaidBalance, onDemandCap, onDemandUsed

Cached to ~/.cache/grok-usage.json so the menu still shows last-known usage
when Grok hasn't run recently.

Imported by claude-usage.5m.py. The leading underscore tells SwiftBar/xbar
not to try to execute this file as a standalone plugin.
"""

import datetime as dt
import json
import os
from pathlib import Path

LOG_FILE = Path(os.environ.get("GROK_LOG_FILE")
                or Path.home() / ".grok" / "logs" / "unified.jsonl")
CACHE = Path(os.environ.get("GROK_USAGE_CACHE")
             or Path.home() / ".cache" / "grok-usage.json")


def _latest_snapshot():
    """Scan the Grok unified log for the freshest billing config event."""
    if not LOG_FILE.exists():
        return None

    best = None
    try:
        # Read the file to find the latest billing event
        with open(LOG_FILE, "r", errors="ignore") as f:
            for line in f:
                if "billing: fetched credits config" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                cfg = (rec.get("ctx") or {}).get("config") or {}
                if cfg.get("creditUsagePercent") is not None:
                    best = rec
    except OSError:
        return None

    if not best:
        return None

    ctx = best.get("ctx") or {}
    cfg = ctx.get("config") or {}
    cur_period = cfg.get("currentPeriod") or {}
    resets_at = cur_period.get("end")
    pct = cfg.get("creditUsagePercent")
    plan_type = ctx.get("subscriptionTier") or "Grok"

    prepaid = cfg.get("prepaidBalance", {}).get("val", 0)
    ondemand_cap = cfg.get("onDemandCap", {}).get("val", 0)
    ondemand_used = cfg.get("onDemandUsed", {}).get("val", 0)

    return {
        "weekly": {
            "pct": float(pct),
            "resets_at": resets_at,
            "window_minutes": 10080,
        },
        "session": None,
        "extra": {
            "prepaid_balance_cents": prepaid,
            "balance_cents": prepaid,
            "on_demand_cap_cents": ondemand_cap,
            "on_demand_used_cents": ondemand_used,
        },
        "plan_type": plan_type,
        "snapshot_ts": best.get("ts"),
    }


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
    """Freshest Grok usage snapshot, or last cached one.

    Returns a dict with:
        weekly  {pct, resets_at, window_minutes}
        session None
        extra   {prepaid_balance_cents, on_demand_cap_cents, on_demand_used_cents}
        plan_type, snapshot_ts, fetched_at, from_cache
    or None if Grok usage has never been seen.
    """
    snap = _latest_snapshot()
    if snap is not None:
        data = {
            **snap,
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
