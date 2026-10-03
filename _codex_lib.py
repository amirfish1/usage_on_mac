"""Codex usage via the supported app-server account/rateLimits/read method.

A short-lived stdio client uses Codex's existing sign-in without reading tokens.
Rollouts and the last successful cache provide an explicitly stale fallback;
expired windows are never displayed as current usage.
"""

import datetime as dt
import glob
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

SESSIONS_DIR = Path(os.environ.get("CODEX_SESSIONS_DIR")
                    or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions")
CACHE = Path(os.environ.get("CODEX_USAGE_CACHE")
             or Path.home() / ".cache" / "codex-usage.json")
SCAN_DAYS = 9
LIVE_TIMEOUT = 8
STALE_AFTER = 15 * 60


def _codex_command():
    override = os.environ.get("CODEX_USAGE_BIN")
    if override:
        return override
    found = shutil.which("codex")
    if found:
        return found
    # xbar does not inherit the interactive shell's PATH.
    for path in (Path.home() / ".local/bin/codex", Path("/opt/homebrew/bin/codex"),
                 Path("/usr/local/bin/codex")):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _read_live_rate_limits():
    # A supplied fixture directory means an offline reader unless explicitly enabled.
    if os.environ.get("CODEX_USAGE_LIVE", "0" if "CODEX_SESSIONS_DIR" in os.environ else "1") == "0":
        return None
    command = _codex_command()
    if not command:
        return None
    proc = None
    try:
        proc = subprocess.Popen([command, "app-server", "--stdio"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        def send(message):
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()

        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "usage_on_mac", "version": "1.0"}}})
        deadline = time.monotonic() + LIVE_TIMEOUT
        buffer = b""
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == 1:
                        if "error" in message:
                            return None
                        send({"method": "initialized"})
                        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
                    elif message.get("id") == 2:
                        result = message.get("result") or {}
                        buckets = result.get("rateLimitsByLimitId")
                        if isinstance(buckets, dict):
                            return buckets.get("codex")
                        legacy = result.get("rateLimits") or {}
                        return legacy if legacy.get("limitId", "codex") == "codex" else None
    except (OSError, ValueError, TypeError):
        return None
    finally:
        if proc is not None:
            # The CLI may be a Node wrapper: terminate only our own process group.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=1)
            except ProcessLookupError:
                proc.wait()
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            for stream in (proc.stdin, proc.stdout):
                stream.close()
    return None


def _iter_recent_rollouts(scan_days=SCAN_DAYS):
    cutoff = time.time() - scan_days * 86400
    for path in glob.glob(str(SESSIONS_DIR / "*" / "*" / "*" / "rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) >= cutoff:
                yield path
        except OSError:
            continue


def _epoch(timestamp):
    try:
        return dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return 0


def _latest_snapshot():
    best = None
    for path in _iter_recent_rollouts():
        try:
            with open(path, "r", errors="ignore") as file:
                for line in file:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                        rl = (rec.get("payload") or {}).get("rate_limits")
                        ts = rec.get("timestamp")
                        if (isinstance(rl, dict) and rl.get("limit_id", "codex") == "codex"
                                and _window_for_duration(rl, 10080) and _epoch(ts)
                                and (best is None or _epoch(ts) > _epoch(best[0]))):
                            best = ts, rl
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            continue
    return best


def _window(rl, key):
    w = rl.get(key) or {}
    try:
        pct = float(w.get("used_percent", w.get("usedPercent")))
        resets = int(w.get("resets_at", w.get("resetsAt")))
        minutes = int(w.get("window_minutes", w.get("windowDurationMins")))
        if not math.isfinite(pct) or not 0 <= pct <= 100:
            return None
        return {"pct": pct, "resets_at": resets, "window_minutes": minutes}
    except (ValueError, TypeError, AttributeError):
        return None


def _window_for_duration(rl, window_minutes):
    for key in ("primary", "secondary"):
        window = _window(rl, key)
        if window and window["window_minutes"] == window_minutes:
            return window
    return None


def _write_cache(data):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data))
    except OSError:
        pass


def _read_cache():
    try:
        data = json.loads(CACHE.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _snapshot_data(ts, rl, source):
    weekly = _window_for_duration(rl, 10080)
    if weekly is None:
        return None
    return {"weekly": weekly, "session": _window_for_duration(rl, 300),
            "plan_type": rl.get("plan_type", rl.get("planType")),
            "snapshot_ts": ts, "fetched_at": _epoch(ts),
            "from_cache": source != "app-server" and time.time() - _epoch(ts) > STALE_AFTER,
            "source": source}


def valid_usage(data):
    """Drop expired/invalid windows, including CCC's ISO reset format."""
    if not isinstance(data, dict):
        return None
    result = dict(data)
    for key in ("weekly", "session"):
        window = data.get(key)
        try:
            reset = window["resets_at"]
            reset = _epoch(reset) if isinstance(reset, str) else float(reset)
            pct = float(window["pct"])
            if reset <= time.time() or not math.isfinite(pct) or not 0 <= pct <= 100:
                window = None
        except (TypeError, KeyError, ValueError):
            window = None
        result[key] = window
    return result if result["weekly"] else None


def read_usage():
    """Live usage, or the newest unexpired local snapshot marked when stale."""
    live = _read_live_rate_limits()
    if isinstance(live, dict):
        ts = dt.datetime.now(dt.timezone.utc).isoformat()
        data = valid_usage(_snapshot_data(ts, live, "app-server"))
        if data:
            _write_cache(data)
            return data
    cached = valid_usage(_read_cache())
    snap = _latest_snapshot()
    local = valid_usage(_snapshot_data(*snap, "rollout")) if snap else None
    if cached:
        cached["from_cache"] = True
    # Polling an old rollout must not overwrite a newer live cache.
    if local and (not cached or _epoch(local.get("snapshot_ts")) > _epoch(cached.get("snapshot_ts"))):
        _write_cache(local)
        return local
    return cached
