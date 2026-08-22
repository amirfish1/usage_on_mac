"""Kimi (Moonshot AI "Kimi Code" CLI) weekly/session usage via the usages API.

Kimi Code stores OAuth credentials at ~/.kimi-code/credentials/kimi-code.json
(access_token, expires_at epoch seconds, ...). The CLI refreshes the stored
token itself whenever it runs, so we just use the access_token as-is — no
refresh flow here. GET {base}/usages (base defaults to
https://api.kimi.com/coding/v1, overridable via KIMI_CODE_BASE_URL) returns
the same numbers shown by the CLI's /usage command:

    user.membership.level   — e.g. "LEVEL_ADVANCED" (plan tier)
    usage                   — weekly: limit/used/remaining/resetTime (strings)
    limits[]                — per-window limits; the 300-minute (5h) entry is
                              the current session limit
    boosterWallet           — extra-usage booster balance + monthly charge cap
                              (amount is fixed-point: cents = amount / 1e6)

Cached to ~/.cache/kimi-usage.json so the menu still shows last-known usage
when the fetch fails (401, offline, malformed response).

Imported by claude-usage.5m.py (SwiftBar). The leading underscore tells
SwiftBar not to try to execute this file.
"""

import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path

CREDENTIALS = Path(os.environ.get("KIMI_CREDENTIALS_FILE")
                   or Path.home() / ".kimi-code" / "credentials" / "kimi-code.json")
CACHE = Path(os.environ.get("KIMI_USAGE_CACHE")
             or Path.home() / ".cache" / "kimi-usage.json")
BASE_URL = os.environ.get("KIMI_CODE_BASE_URL", "https://api.kimi.com/coding/v1").rstrip("/")
TIMEOUT = 8  # seconds

_UNIT_MINUTES = {
    "TIME_UNIT_MINUTE": 1,
    "TIME_UNIT_HOUR": 60,
    "TIME_UNIT_DAY": 1440,
}


def _to_int(v):
    """API numeric fields arrive as strings; accept ints/floats too."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_reset_time(s):
    """ISO 8601 (fractional seconds, Z) or epoch → epoch float, or None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        text = str(s).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # fromisoformat caps fractional seconds at 6 digits — truncate longer
        m = re.match(r"^(.*\.\d{6})\d+(.*)$", text)
        if m:
            text = m.group(1) + m.group(2)
        return dt.datetime.fromisoformat(text).timestamp()
    except Exception:
        pass
    try:
        return dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except Exception:
        return None


def _reset_field(d):
    for key in ("resetTime", "reset_at", "resetAt", "reset_time"):
        if d.get(key) is not None:
            return d[key]
    return None


def _usage_window(u):
    """{pct, used, limit, resets_at} from a usage/detail object, or None."""
    if not isinstance(u, dict):
        return None
    limit = _to_int(u.get("limit"))
    if not limit:
        return None
    used = _to_int(u.get("used"))
    if used is None:
        remaining = _to_int(u.get("remaining"))
        if remaining is None:
            return None
        used = limit - remaining
    return {
        "pct": used / limit * 100,
        "used": used,
        "limit": limit,
        "resets_at": _parse_reset_time(_reset_field(u)),
    }


def _window_minutes(window):
    duration = _to_int((window or {}).get("duration"))
    if duration is None:
        return None
    unit = str((window or {}).get("timeUnit") or "").upper()
    return duration * _UNIT_MINUTES.get(unit, 1)


def _session_window(limits):
    """The 5h (300-minute) entry from limits[], else the smallest window."""
    candidates = []
    for entry in limits or []:
        if not isinstance(entry, dict):
            continue
        minutes = _window_minutes(entry.get("window"))
        parsed = _usage_window(entry.get("detail"))
        if minutes and parsed:
            candidates.append((minutes, parsed))
    if not candidates:
        return None
    for minutes, parsed in candidates:
        if minutes == 300:
            return parsed
    return min(candidates, key=lambda c: c[0])[1]


def _money_cents(m):
    if not isinstance(m, dict):
        return None
    return _to_int(m.get("priceInCents"))


def _extra_from_wallet(wallet):
    """Booster-wallet extra usage, or None when absent/not a booster."""
    if not isinstance(wallet, dict):
        return None
    balance = wallet.get("balance") or {}
    if balance.get("type") != "BOOSTER":
        return None
    amount = _to_int(balance.get("amount"))
    if amount is None:
        return None
    total_cents = amount / 1_000_000
    if 0 < total_cents < 1:
        total_cents = 1
    else:
        total_cents = round(total_cents)
    left = _to_int(balance.get("amountLeft"))
    balance_cents = None
    if left is not None:
        balance_cents = left / 1_000_000
        balance_cents = 1 if 0 < balance_cents < 1 else round(balance_cents)
    charge_limit = wallet.get("monthlyChargeLimit") or {}
    monthly_used = wallet.get("monthlyUsed") or {}
    currency = charge_limit.get("currency") or monthly_used.get("currency") or "USD"
    return {
        "total_cents": total_cents,
        "balance_cents": balance_cents,
        "monthly_used_cents": _money_cents(monthly_used) or 0,
        "monthly_limit_cents": _money_cents(charge_limit) or 0,
        "monthly_limit_enabled": bool(wallet.get("monthlyChargeLimitEnabled")),
        "currency": currency,
    }


def _pretty_level(level):
    """"LEVEL_ADVANCED" → "Advanced"."""
    if not level:
        return None
    s = str(level)
    if s.startswith("LEVEL_"):
        s = s[len("LEVEL_"):]
    return s.replace("_", " ").title() or None


def _load_access_token():
    creds = json.loads(CREDENTIALS.read_text())
    token = creds.get("access_token")
    if not token:
        raise ValueError("no access_token in credentials")
    return token


def _fetch(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8"))


def _parse(data):
    """Normalize the /usages response, or None if weekly can't be derived."""
    if not isinstance(data, dict):
        return None
    weekly = _usage_window(data.get("usage"))
    if weekly is None:
        return None
    return {
        "weekly": weekly,
        "session": _session_window(data.get("limits")),
        "extra": _extra_from_wallet(data.get("boosterWallet")),
        "plan_type": _pretty_level(((data.get("user") or {}).get("membership") or {}).get("level")),
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
    """Freshest Kimi usage snapshot, or last cached one. Returns a dict with:
        weekly  {pct, used, limit, resets_at}
        session {pct, used, limit, resets_at}  (may be None)
        extra   {total_cents, balance_cents, monthly_used_cents,
                 monthly_limit_cents, monthly_limit_enabled, currency}  (may be None)
        plan_type, fetched_at, from_cache
    or None if Kimi usage has never been fetched successfully."""
    try:
        token = _load_access_token()
        data = _fetch(f"{BASE_URL}/usages", token)
        usage = _parse(data)
        if usage is not None:
            usage["fetched_at"] = dt.datetime.now().timestamp()
            usage["from_cache"] = False
            _write_cache(usage)
            return usage
    except Exception:
        pass
    cached = _read_cache()
    if cached:
        cached["from_cache"] = True
        return cached
    return None
