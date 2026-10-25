"""Antigravity weekly/session usage from the agy CLI.

Two billing modes:
  - Subscription (consumer login): `agy --print "/usage" --output-format json`
    returns quota buckets with remaining fractions and reset times.
  - Vertex pay-as-you-go (`gcp.project` set in ~/.gemini/antigravity-cli/settings.json):
    agy has no quota, so `/usage` returns no groups. We read real rolling
    7-day token counts from Cloud Monitoring
    (aiplatform.googleapis.com/publisher/online_serving/token_count) with the
    gcloud ADC token, estimate spend, and report it as a % of a weekly budget
    from ~/.config/usage_on_mac/antigravity.json ({"weekly_budget_usd": N}).

Cached to ~/.cache/antigravity-usage.json.
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

CACHE = Path(os.environ.get("ANTIGRAVITY_USAGE_CACHE")
             or Path.home() / ".cache" / "antigravity-usage.json")
TIMEOUT = 8  # seconds
AGY_SETTINGS = Path(os.environ.get("ANTIGRAVITY_SETTINGS")
                    or Path.home() / ".gemini" / "antigravity-cli" / "settings.json")
CONFIG = Path(os.environ.get("ANTIGRAVITY_USAGE_CONFIG")
              or Path.home() / ".config" / "usage_on_mac" / "antigravity.json")
# Flat per-1M-token rates, same as agy's cost-status.sh "Max Est" (cached
# input is billed lower, so this is an upper bound). Override in CONFIG.
DEFAULT_INPUT_PER_M = 2.0
DEFAULT_OUTPUT_PER_M = 10.0
VERTEX_WINDOW_DAYS = 7
TOKEN_METRIC = "aiplatform.googleapis.com/publisher/online_serving/token_count"

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

def _gcp_project():
    """GCP project agy bills to, or None when agy uses a subscription login."""
    try:
        settings = json.loads(AGY_SETTINGS.read_text())
    except Exception:
        return None
    gcp = settings.get("gcp") if isinstance(settings, dict) else None
    project = gcp.get("project") if isinstance(gcp, dict) else None
    return project or None


def read_config():
    try:
        data = json.loads(CONFIG.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_config(data):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, indent=2) + "\n")


def _gcloud_token():
    gcloud = shutil.which("gcloud") or "/opt/homebrew/bin/gcloud"
    try:
        proc = subprocess.run(
            [gcloud, "auth", "application-default", "print-access-token"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except Exception:
        return None
    token = (proc.stdout or "").strip()
    return token if proc.returncode == 0 and token else None


def _fetch_vertex_tokens(project, days=VERTEX_WINDOW_DAYS):
    """Summed input/output tokens over the last `days` days, or None."""
    token = _gcloud_token()
    if not token:
        return None
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    start = end - dt.timedelta(days=days)
    params = [
        ("filter", f'metric.type="{TOKEN_METRIC}"'),
        ("interval.startTime", start.isoformat().replace("+00:00", "Z")),
        ("interval.endTime", end.isoformat().replace("+00:00", "Z")),
        ("aggregation.alignmentPeriod", f"{days * 86400}s"),
        ("aggregation.perSeriesAligner", "ALIGN_SUM"),
        ("aggregation.crossSeriesReducer", "REDUCE_SUM"),
        ("aggregation.groupByFields", "metric.label.type"),
    ]
    url = (f"https://monitoring.googleapis.com/v3/projects/{urllib.parse.quote(project)}/timeSeries?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    return _parse_vertex_tokens(data)


def _parse_vertex_tokens(data):
    if not isinstance(data, dict) or "error" in data:
        return None
    totals = {"input": 0, "output": 0}
    for series in data.get("timeSeries") or []:
        kind = ((series.get("metric") or {}).get("labels") or {}).get("type")
        if kind not in totals:
            continue
        for point in series.get("points") or []:
            try:
                totals[kind] += int((point.get("value") or {}).get("int64Value") or 0)
            except (TypeError, ValueError):
                pass
    return totals


def _vertex_usage(project, tokens, config):
    """Normalize Vertex token totals into the read_usage() shape."""
    in_rate = float(config.get("input_per_m_usd") or DEFAULT_INPUT_PER_M)
    out_rate = float(config.get("output_per_m_usd") or DEFAULT_OUTPUT_PER_M)
    cost = tokens["input"] / 1e6 * in_rate + tokens["output"] / 1e6 * out_rate
    budget = config.get("weekly_budget_usd")
    try:
        budget = float(budget) if budget else None
    except (TypeError, ValueError):
        budget = None
    pct = cost / budget * 100.0 if budget else None
    return {
        "billing": "vertex",
        "gemini": {"weekly": {"pct": pct, "resets_at": None}},
        "vertex": {
            "project": project,
            "window_days": VERTEX_WINDOW_DAYS,
            "input_tokens": tokens["input"],
            "output_tokens": tokens["output"],
            "est_cost_usd": cost,
            "weekly_budget_usd": budget,
        },
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

    In Vertex mode the shape is {"billing": "vertex", "gemini": {"weekly":
    {"pct": float|None, "resets_at": None}}, "vertex": {...}} (pct is None
    when no weekly budget is configured).
    """
    project = _gcp_project()
    if project:
        tokens = _fetch_vertex_tokens(project)
        if tokens is not None:
            data = _vertex_usage(project, tokens, read_config())
            data["fetched_at"] = dt.datetime.now().timestamp()
            data["from_cache"] = False
            _write_cache(data)
            return data
        cached = _read_cache()
        # Never fall back to a subscription-era snapshot: its quota no longer applies.
        if cached and cached.get("billing") == "vertex":
            cached["from_cache"] = True
            return cached
        return None

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
