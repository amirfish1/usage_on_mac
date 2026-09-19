"""Devin (Cognition) daily/weekly quota usage via Devin Desktop's credentials.

Devin Desktop (the Windsurf-based app + `devin` CLI) stores a session token in
`~/.local/share/devin/credentials.toml`. The same backend the CLI calls,
`<api_server_url>/exa.seat_management_pb.SeatManagementService/GetUserStatus`,
returns the plan's quota status:

  - weeklyQuotaRemainingPercent / weeklyQuotaResetAtUnix
  - dailyQuotaRemainingPercent  / dailyQuotaResetAtUnix
  - overageBalanceMicros        — USD overage balance (micros = 1e-6 dollars)
  - planInfo.planName           — e.g. "Pro"

Cached to ~/.cache/devin-usage.json so the menu still shows last-known usage
when the endpoint is unreachable or the session token has expired.

Imported by claude-usage.5m.py. The leading underscore tells SwiftBar/xbar
not to try to execute this file as a standalone plugin.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

CREDENTIALS_FILE = Path(os.environ.get("DEVIN_CREDENTIALS_FILE")
                        or Path.home() / ".local" / "share" / "devin" / "credentials.toml")
CACHE = Path(os.environ.get("DEVIN_USAGE_CACHE")
             or Path.home() / ".cache" / "devin-usage.json")
STATUS_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"
TIMEOUT = 6


def _read_credentials():
    """(api_key, api_server_url) from credentials.toml, or None.

    Parsed with regex rather than tomllib so it works on macOS's stock
    Python 3.9. Returns None if the file or key is missing."""
    try:
        text = CREDENTIALS_FILE.read_text()
    except OSError:
        return None
    key = re.search(r'^\s*windsurf_api_key\s*=\s*"([^"]+)"', text, re.M)
    if not key:
        return None
    url = re.search(r'^\s*api_server_url\s*=\s*"([^"]+)"', text, re.M)
    return key.group(1), (url.group(1) if url else "https://server.codeium.com")


def _fetch_user_status():
    """Live planStatus dict from GetUserStatus, or None on any failure."""
    creds = _read_credentials()
    if not creds:
        return None
    api_key, base_url = creds
    body = json.dumps({
        "metadata": {
            "apiKey": api_key,
            "ideName": "devin",
            "ideVersion": "1.0",
            "extensionName": "devin-cli",
            "extensionVersion": "1.12.0",
        }
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + STATUS_PATH,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            data = json.loads(res.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    status = (data.get("userStatus") or {}).get("planStatus") or {}
    if status.get("weeklyQuotaRemainingPercent") is None:
        return None
    return status


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


def _window(remaining_pct, reset_unix, window_minutes):
    """{pct used, resets_at epoch, window_minutes} or None if the field is absent."""
    if remaining_pct is None:
        return None
    return {
        "pct": max(0.0, min(100.0, 100.0 - float(remaining_pct))),
        "resets_at": int(reset_unix) if reset_unix else None,
        "window_minutes": window_minutes,
    }


def read_usage():
    """Freshest Devin quota snapshot, or last cached one.

    Returns a dict with:
        weekly   {pct, resets_at, window_minutes}   — weekly quota (7d window)
        session  {pct, resets_at, window_minutes}   — daily quota (24h window)
        extra    {overage_balance_micros, plan_start, plan_end}
        plan_type, fetched_at, from_cache
    or None if Devin usage has never been seen.
    """
    status = _fetch_user_status()
    if status is not None:
        weekly = _window(status.get("weeklyQuotaRemainingPercent"),
                         status.get("weeklyQuotaResetAtUnix"), 10080)
        daily = _window(status.get("dailyQuotaRemainingPercent"),
                        status.get("dailyQuotaResetAtUnix"), 1440)
        data = {
            "weekly": weekly,
            "session": daily,
            "extra": {
                "overage_balance_micros": status.get("overageBalanceMicros"),
                "plan_start": status.get("planStart"),
                "plan_end": status.get("planEnd"),
            },
            "plan_type": (status.get("planInfo") or {}).get("planName"),
            "fetched_at": time.time(),
            "from_cache": False,
        }
        _write_cache(data)
        return data

    cached = _read_cache()
    if cached:
        cached["from_cache"] = True
        return cached

    return None
