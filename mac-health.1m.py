#!/usr/bin/env python3
# <swiftbar.title>Mac Health</swiftbar.title>
# <swiftbar.author>amir</swiftbar.author>
# <swiftbar.desc>Load, memory pressure, process counts, and cleanup suggestions.</swiftbar.desc>
# <swiftbar.dependencies>python3, top, ps, pgrep</swiftbar.dependencies>
#
# Refresh: 1m (filename: mac-health.1m.py)
#
# Headline: emoji + 1m load avg, colored by pressure tier.
# Dropdown: load triplet, CPU idle, memory split, total processes,
# counts for things that tend to leak (cozempic, calendly-mcp-server,
# claude CLI sessions, node, npm), and ranked memory cleanup suggestions.

import datetime
import json
import os
import re
import shlex
import subprocess
import time
import urllib.request

C  = "color=#1a1a1a,#ffffff"
CD = "color=#666666,#cfcfcf"
OK = "color=#0a7d20,#5dd66d"
WARN = "color=#b8860b,#e6c200"
BAD = "color=#c0392b,#ff7b7b"

TOP = "/usr/bin/top"
PGREP = "/usr/bin/pgrep"
PS = "/bin/ps"
SYSCTL = "/usr/sbin/sysctl"
VM_STAT = "/usr/bin/vm_stat"
LSOF = "/usr/sbin/lsof"

# Claude CLI sessions each anchor a subtree (chrome-devtools-mcp, node, python,
# uv …) that can hold hundreds of MB. We reap by the whole tree, but never a
# session that's interactive (a TTY = a human is in it) or driving real work
# (a transcode/encode child is the tell). Caveat: a *bursty* worker (e.g.
# whisper run chunk-by-chunk) is invisible during the few-second gaps between
# chunks; the worker runs minutes per chunk, so it's protected almost always,
# and reaping is always a deliberate click — but the gap is a known blind spot.
STALE_SESSION_MIN = 120     # age past which an idle session is "stale"
SESSION_BUSY_CPU = 30.0     # tree CPU% above which a session counts as working
WORKER_RE = re.compile(r"\b(whisper(?:-cli)?|ffmpeg|ffprobe|HandBrakeCLI)\b", re.I)

# Mach kernel memory pressure levels (from sys/kern_memorystatus.h):
# 1 = NORMAL, 2 = WARN, 4 = CRITICAL. Matches Activity Monitor's pressure bar.
KERNEL_PRESSURE = {1: "ok", 2: "elevated", 4: "critical"}

UNIT_MB = {
    "K": 1 / 1024,
    "M": 1,
    "G": 1024,
    "T": 1024 * 1024,
}

ZOMBIE_PATTERNS = [
    ("cozempic", "cozempic"),
    ("calendly-mcp-server", "calendly-mcp-server"),
]

# Claude Command Center (CCC) — the local orchestration daemon. Its health is
# dominated by the /api/sessions/live-activity poll path, so we watch the
# server process CPU, probe that endpoint's latency, and count recent errors.
CCC_SERVER_MATCH = "claude-command-center/server.py"
CCC_LIVE_URL = "http://localhost:8090/api/sessions/live-activity"
CCC_ERR_LOG = os.path.expanduser("~/.claude/command-center/logs/service.err.log")


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def parse_top():
    out = run([TOP, "-l", "1", "-n", "0"])
    g = lambda pat, default=None: (m.groups() if (m := re.search(pat, out)) else default)
    procs = g(r"Processes:\s+(\d+) total.*?(\d+) threads") or (None, None)
    load  = g(r"Load Avg:\s+([\d.]+),\s+([\d.]+),\s+([\d.]+)") or (None, None, None)
    cpu   = g(r"CPU usage:.*?([\d.]+)% idle")
    mem   = re.search(
        r"PhysMem:\s+([\d.]+)([KMGT]) used "
        r"\(([\d.]+)([KMGT]) wired,\s+([\d.]+)([KMGT]) compressor\),\s+"
        r"([\d.]+)([KMGT]) unused",
        out,
    )
    return {
        "procs":   int(procs[0]) if procs[0] else None,
        "threads": int(procs[1]) if procs[1] else None,
        "load":    [float(x) for x in load] if load[0] else [None, None, None],
        "cpu_idle": float(cpu[0]) if cpu else None,
        "mem":     {
            "used_mb": to_mb(mem.group(1), mem.group(2)),
            "wired_mb": to_mb(mem.group(3), mem.group(4)),
            "compressed_mb": to_mb(mem.group(5), mem.group(6)),
            "free_mb": to_mb(mem.group(7), mem.group(8)),
        } if mem else None,
    }


def parse_vm_stat():
    """Page-accurate memory accounting. macOS reports cached/purgeable as 'used'
    in top, which inflates pressure; vm_stat + the kernel pressure sysctl match
    Activity Monitor's bar."""
    out = run([VM_STAT])
    if not out:
        return None
    try:
        page_size = int(run([SYSCTL, "-n", "hw.pagesize"]).strip() or 16384)
    except ValueError:
        page_size = 16384
    pages = {}
    for line in out.splitlines():
        m = re.match(r"(.+?):\s+(\d+)\.?\s*$", line.strip())
        if m:
            pages[m.group(1).strip()] = int(m.group(2))
    mb = lambda key: pages.get(key, 0) * page_size / (1024 * 1024)
    free = mb("Pages free")
    inactive = mb("Pages inactive")
    speculative = mb("Pages speculative")
    purgeable = mb("Pages purgeable")
    return {
        "free_mb": free,
        "inactive_mb": inactive,
        "speculative_mb": speculative,
        "purgeable_mb": purgeable,
        "wired_mb": mb("Pages wired down"),
        "compressed_mb": mb("Pages occupied by compressor"),
        "available_mb": free + inactive + speculative + purgeable,
    }


def swap_usage():
    """macOS swap from vm.swapusage. Heavy swap is the real 'why is everything
    slow' signal — pressure can read OK while the box thrashes through swap."""
    out = run([SYSCTL, "-n", "vm.swapusage"]).strip()
    pairs = re.findall(r"(total|used|free)\s*=\s*([\d.]+)([KMGT])", out)
    if not pairs:
        return None
    d = {k: to_mb(v, u) for k, v, u in pairs}
    if "total" not in d:
        return None
    return d


def kernel_pressure_level():
    out = run([SYSCTL, "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    try:
        return KERNEL_PRESSURE.get(int(out))
    except ValueError:
        return None


def cpu_count():
    out = run([SYSCTL, "-n", "hw.ncpu"]).strip()
    try:
        return int(out)
    except ValueError:
        return 1


def total_mem_mb():
    out = run([SYSCTL, "-n", "hw.memsize"]).strip()
    try:
        return int(out) / (1024 * 1024)
    except ValueError:
        return None


def count(pgrep_args):
    out = run([PGREP] + pgrep_args)
    return len([l for l in out.splitlines() if l.strip()])


def to_mb(value, unit):
    return float(value) * UNIT_MB.get(unit.upper(), 1)


def parse_etime(s):
    """ps etime: SS, MM:SS, HH:MM:SS, or DD-HH:MM:SS -> minutes."""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return 0.0
    try:
        nums = [int(p) for p in s.split(":")]
    except ValueError:
        return 0.0
    if len(nums) == 1:
        h, m, sec = 0, 0, nums[0]
    elif len(nums) == 2:
        h, m, sec = 0, nums[0], nums[1]
    else:
        h, m, sec = nums[0], nums[1], nums[2]
    return days * 1440 + h * 60 + m + sec / 60.0


def fmt_etime(mins):
    if mins < 60:
        return f"{mins:.0f}m"
    if mins < 1440:
        return f"{mins / 60:.1f}h"
    return f"{mins / 1440:.1f}d"


def fmt_mb(mb):
    if mb is None:
        return "?"
    if mb >= 1024:
        gb = mb / 1024
        return f"{gb:.0f} GB" if gb >= 10 else f"{gb:.1f} GB"
    return f"{mb:.0f} MB"


def fmt_mem(mem):
    if not mem:
        return "?", "?", "?"
    return fmt_mb(mem["used_mb"]), fmt_mb(mem.get("available_mb", mem["free_mb"])), fmt_mb(mem["compressed_mb"])


def memory_pressure(mem, total_mb, kernel_level=None):
    """Prefer the kernel's own pressure value (what Activity Monitor's bar reflects).
    Fall back to a heuristic on available memory (free + inactive + speculative +
    purgeable) — NOT 'unused' from top, which excludes purgeable cache and
    over-reports pressure on macOS."""
    if not mem or not total_mb:
        return "unknown", "pressure: unknown", CD

    avail_mb = mem.get("available_mb", mem.get("free_mb", 0))
    avail_pct = avail_mb / total_mb if total_mb else 0
    comp_pct = mem["compressed_mb"] / total_mb if total_mb else 0

    if kernel_level:
        level = kernel_level
        if level == "ok":
            color = OK
        elif level == "elevated":
            color = WARN
        else:
            color = BAD
        source = "kernel"
    else:
        if avail_pct < 0.05:
            level, color = "critical", BAD
        elif avail_pct < 0.10:
            level, color = "high", BAD
        elif avail_pct < 0.20:
            level, color = "elevated", WARN
        else:
            level, color = "ok", OK
        source = "available"

    summary = (
        f"pressure: {level} "
        f"(avail {avail_pct * 100:.0f}% · compressed {comp_pct * 100:.0f}% · src {source})"
    )
    return level, summary, color


def level_rank(level):
    return {"ok": 0, "elevated": 1, "high": 2, "critical": 3, "unknown": 0}.get(level, 0)


def level_emoji(level):
    if level == "ok":
        return "🟢"
    if level == "elevated":
        return "🟡"
    if level == "high":
        return "🟠"
    return "🔴"


def load_level(norm):
    if norm is None:
        return "unknown"
    if norm < 0.7:
        return "ok"
    if norm < 1.0:
        return "elevated"
    if norm < 2.0:
        return "high"
    return "critical"


def headline(load_1m, cores, mem_level, hog_level, hog_count, ccc_level="ok"):
    """Color by max(load-per-core, mem, cpu-hog, CCC). Load avg is honest at
    the box level, but a single 109% runaway hides behind a low aggregate
    load on a multi-core box, so per-process CPU has to feed in too. CCC's
    health rides along so a bad daemon shows without opening the menu."""
    suffix = f" · {hog_count} hog{'' if hog_count == 1 else 's'}" if hog_count else ""
    ccc_tag = " · CCC" if level_rank(ccc_level) >= 2 else ""
    if load_1m is None:
        overall = max((mem_level, hog_level, ccc_level), key=level_rank)
        emoji = level_emoji(overall) if level_rank(overall) else "⚪"
        return f"{emoji} mem{suffix}{ccc_tag}", None
    norm = load_1m / cores
    overall = max((load_level(norm), mem_level, hog_level, ccc_level), key=level_rank)
    emoji = level_emoji(overall)
    return f"{emoji} {load_1m:.1f}{suffix}{ccc_tag}", norm


def process_rows():
    out = run([PS, "-axo", "pid=,ppid=,rss=,pcpu=,etime=,tty=,command="])
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_mb = int(parts[2]) / 1024
            cpu = float(parts[3])
            etime_min = parse_etime(parts[4])
        except ValueError:
            continue
        tty = parts[5]
        cmd = parts[6]
        if pid == os.getpid() or "mac-health.1m.py" in cmd:
            continue
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "rss_mb": rss_mb,
            "cpu": cpu,
            "etime_min": etime_min,
            "tty": tty,
            "cmd": cmd,
            "label": process_label(cmd),
        })
    return rows


def process_label(cmd):
    exe = cmd.split(None, 1)[0]
    base = os.path.basename(exe)
    if "python" in exe.lower():
        script = re.search(r"\s([^ ]+\.py)(?:\s|$)", cmd)
        return f"Python: {os.path.basename(script.group(1))}" if script else "Python"

    app = re.search(r"/([^/]+)\.app/Contents/", cmd)
    if app:
        return app.group(1)

    lower = cmd.lower()

    if "calendly-mcp-server" in lower:
        return "calendly-mcp-server"
    if "cozempic" in lower:
        return "cozempic"
    if base == "claude":
        return "claude CLI"
    if base == "codex":
        return "codex"
    if base == "node":
        return "node MCP/dev"
    if base == "npm":
        return "npm"
    return base or cmd[:40]


def memory_groups(rows):
    groups = {}
    for row in rows:
        label = row["label"]
        group = groups.setdefault(label, {"label": label, "rss_mb": 0, "cpu": 0, "count": 0, "pids": []})
        group["rss_mb"] += row["rss_mb"]
        group["cpu"] += row["cpu"]
        group["count"] += 1
        group["pids"].append(row["pid"])
    return sorted(groups.values(), key=lambda g: g["rss_mb"], reverse=True)


def top_memory_processes(rows, limit=6):
    return sorted(rows, key=lambda p: p["rss_mb"], reverse=True)[:limit]


def _descendants(root, children):
    seen, stack = [], list(children.get(root, []))
    while stack:
        pid = stack.pop()
        seen.append(pid)
        stack.extend(children.get(pid, []))
    return seen


def session_cwds(pids):
    """Batch one lsof for the cwd of every session root — used to tell the
    sessions apart by repo. Cheap enough at ~10 roots; skipped if none."""
    if not pids:
        return {}
    out = run([LSOF, "-a", "-d", "cwd", "-Fn", "-p", ",".join(map(str, pids))])
    cwds, cur = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                cur = int(line[1:])
            except ValueError:
                cur = None
        elif line.startswith("n") and cur is not None:
            cwds.setdefault(cur, line[1:])
    return cwds


def _is_interactive_tty(tty):
    """A real controlling terminal (ttys000…) means a human is in this session;
    headless CCC-spawned `claude -p` sessions show '??'. Never reap a terminal."""
    return bool(tty) and tty not in ("??", "?", "-", "")


def claude_sessions(rows):
    """Each claude-CLI root rolled up with its whole subtree's RSS/CPU. A
    session is only reapable when it is headless (no TTY), idle (no
    encode/transcode child, low CPU), and stale (old) — so a terminal you're
    in or a session driving work is never offered for reaping."""
    by_pid = {r["pid"]: r for r in rows}
    children = {}
    for r in rows:
        children.setdefault(r["ppid"], []).append(r["pid"])

    sessions = []
    for r in rows:
        if r["label"] != "claude CLI":
            continue
        desc = _descendants(r["pid"], children)
        desc_rows = [by_pid[p] for p in desc if p in by_pid]
        tree_rss = r["rss_mb"] + sum(d["rss_mb"] for d in desc_rows)
        tree_cpu = r["cpu"] + sum(d["cpu"] for d in desc_rows)
        worker = next((d for d in desc_rows if WORKER_RE.search(d["cmd"])), None)
        interactive = _is_interactive_tty(r["tty"])
        busy = bool(worker) or tree_cpu >= SESSION_BUSY_CPU
        reapable = (not interactive) and (not busy) and r["etime_min"] >= STALE_SESSION_MIN
        sessions.append({
            "pid": r["pid"],
            "age_min": r["etime_min"],
            "tree_rss": tree_rss,
            "tree_cpu": tree_cpu,
            "tree_pids": [r["pid"]] + desc,
            "nprocs": 1 + len(desc),
            "interactive": interactive,
            "busy": busy,
            "worker": worker["label"] if worker else None,
            "reapable": reapable,
        })

    cwds = session_cwds([s["pid"] for s in sessions])
    home = os.path.expanduser("~")
    for s in sessions:
        cwd = cwds.get(s["pid"], "")
        if cwd == home:
            s["cwd"] = "~"
        elif cwd:
            s["cwd"] = os.path.basename(cwd) or cwd
        else:
            s["cwd"] = "?"
    return sorted(sessions, key=lambda s: s["tree_rss"], reverse=True)


def cpu_hogs(rows, min_cpu=80, min_etime_min=2):
    """Sustained CPU users. pcpu is a decaying 1-minute average, so
    >=min_cpu means it's been hot recently. etime gate filters brief
    compile/encode spikes that resolve on their own."""
    return sorted(
        [r for r in rows if r["cpu"] >= min_cpu and r["etime_min"] >= min_etime_min],
        key=lambda r: r["cpu"],
        reverse=True,
    )


def cpu_hog_level(hogs):
    if not hogs:
        return "ok"
    top = hogs[0]["cpu"]
    if top >= 200:
        return "critical"
    if top >= 100:
        return "high"
    return "elevated"


def cleanup_suggestions(groups, alarms, hogs):
    suggestions = []

    for hog in hogs[:3]:
        suggestions.append((
            f"Kill CPU hog: {hog['label']} (PID {hog['pid']}, {hog['cpu']:.0f}% for {fmt_etime(hog['etime_min'])})",
            f"kill -TERM {hog['pid']}",
            BAD,
        ))

    for name, n in alarms:
        suggestions.append((
            f"Kill leaked {name} ({n})",
            f"pkill -TERM -f {shlex.quote(name)}",
            BAD,
        ))

    for group in groups:
        if len(suggestions) >= 5:
            break
        if group["rss_mb"] < 400:
            continue

        label = group["label"]
        lower = label.lower()
        size = fmt_mb(group["rss_mb"])

        if "chrome" in lower:
            text = f"Close heavy Chrome tabs/windows ({size})"
        elif label in {"Claude", "Notion", "Slack", "Discord", "Cursor", "Visual Studio Code"}:
            text = f"Quit/reopen {label} if idle ({size})"
        elif label == "claude CLI":
            text = f"Exit stale Claude Code shells after saving work ({size})"
        elif lower in {"node mcp/dev", "npm"} or "mcp" in lower:
            text = f"Stop stale dev/MCP servers ({label}, {size})"
        elif label.startswith("Python:"):
            text = f"Stop local Python job if idle ({label}, {size})"
        elif label not in {"kernel_task", "launchd"}:
            text = f"Quit {label} if idle ({size})"
        else:
            continue

        suggestions.append((text, None, WARN))

    return suggestions


def sh_action(command, refresh=False):
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    attrs = f"shell=/bin/bash param1=-lc param2=\"{escaped}\" terminal=false"
    return f"{attrs} refresh=true" if refresh else attrs


def copy_action(command):
    return sh_action(f"printf %s {shlex.quote(command)} | pbcopy")


def is_user_clearable(row):
    cmd = row["cmd"]
    if cmd.startswith(("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/")):
        return False
    exe = cmd.split(None, 1)[0]
    if "/" not in exe:
        return row["label"] not in {"kernel_task", "launchd"}
    return cmd.startswith(("/Applications/", "/Users/", "/opt/homebrew/")) or row["label"] in {
        "claude CLI",
        "node MCP/dev",
        "npm",
        "codex",
    }


def ccc_server_proc(rows):
    """The CCC daemon row from the process list, or None if it isn't running."""
    for r in rows:
        if CCC_SERVER_MATCH in r["cmd"]:
            return r
    return None


def ccc_probe_live_activity(timeout=2.0):
    """Hit the poll endpoint and time it. Returns
    {ok, ms, sessions, error}. This is the functional health check — it's the
    exact path that pins the CPU when it regresses."""
    t = time.time()
    try:
        with urllib.request.urlopen(CCC_LIVE_URL, timeout=timeout) as resp:
            body = resp.read()
        ms = (time.time() - t) * 1000
        try:
            data = json.loads(body)
            sessions = len(data.get("sessions", {})) if isinstance(data, dict) else None
        except ValueError:
            sessions = None
        return {"ok": True, "ms": ms, "sessions": sessions, "error": None}
    except Exception as e:
        return {"ok": False, "ms": (time.time() - t) * 1000, "sessions": None,
                "error": type(e).__name__}


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
# Access-log stamp: 127.0.0.1 - - [02/Jun/2026 17:24:43] "GET ...
_ACCESS_TS_RE = re.compile(r"\[(\d{2})/([A-Za-z]{3})/(\d{4}) (\d{2}):(\d{2}):(\d{2})\]")


def ccc_recent_errors(window_min=60, tail_bytes=400_000):
    """Count Python tracebacks in the CCC error log within the last
    window_min minutes — catches crash storms (e.g. the sqlite InterfaceError
    class) without going red over stale history (the log persists across
    restarts). Time is taken from the interleaved access-log stamps. None if
    no log."""
    try:
        with open(CCC_ERR_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    cutoff = time.time() - window_min * 60
    recent_ts = None
    count = 0
    for line in data.splitlines():
        m = _ACCESS_TS_RE.search(line)
        if m:
            try:
                recent_ts = datetime.datetime(
                    int(m.group(3)), _MONTHS.get(m.group(2), 1), int(m.group(1)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                ).timestamp()
            except ValueError:
                pass
        elif "Traceback (most recent call last)" in line:
            # Attribute the traceback to the most recent stamp seen above it.
            # Conservative: a traceback with no recent preceding stamp is
            # treated as stale (the log persists across restarts and CCC
            # doesn't stamp every request), so we don't go red over history.
            if recent_ts is not None and recent_ts >= cutoff:
                count += 1
    return count


def ccc_health(proc, probe, errors):
    """Roll the three signals into one level for the headline."""
    if proc is None or not probe["ok"]:
        return "critical"
    level = "ok"
    cpu = proc["cpu"]
    if cpu >= 100:
        level = max(level, "high", key=level_rank)
    elif cpu >= 50:
        level = max(level, "elevated", key=level_rank)
    ms = probe["ms"]
    if ms is not None:
        if ms >= 2000:
            level = max(level, "high", key=level_rank)
        elif ms >= 500:
            level = max(level, "elevated", key=level_rank)
    if errors:
        if errors > 5:
            level = max(level, "high", key=level_rank)
        else:
            level = max(level, "elevated", key=level_rank)
    return level


def main():
    s = parse_top()
    vm = parse_vm_stat()
    if s["mem"] and vm:
        s["mem"]["available_mb"] = vm["available_mb"]
        s["mem"]["compressed_mb"] = vm["compressed_mb"]
    l1, l5, l15 = s["load"]
    used, avail, comp = fmt_mem(s["mem"])
    cores = cpu_count()
    total_mb = total_mem_mb()
    mem_level, mem_summary, mem_color = memory_pressure(s["mem"], total_mb, kernel_pressure_level())
    rows = process_rows()
    groups = memory_groups(rows)
    hogs = cpu_hogs(rows)
    hog_lvl = cpu_hog_level(hogs)

    # CCC daemon health (server CPU, poll-path latency, recent errors)
    ccc_proc = ccc_server_proc(rows)
    ccc_probe = ccc_probe_live_activity()
    ccc_errors = ccc_recent_errors()
    ccc_lvl = ccc_health(ccc_proc, ccc_probe, ccc_errors)

    # Counts
    claude_cli = count(["-x", "claude"])
    node       = count(["-x", "node"])
    npm        = count(["-f", r"^npm "])
    cozempic   = count(["-f", "cozempic"])
    calendly   = count(["-f", "calendly-mcp-server"])
    claude_index = count(["-f", "claude-index mcp"])

    # Headline (load / core, since absolute load is meaningless without core count)
    head, norm = headline(l1, cores, mem_level, hog_lvl, len(hogs), ccc_lvl)
    print(f"{head} | size=12")
    print("---")

    # Load + CPU
    if l1 is not None:
        load_color = OK if norm < 0.7 else (WARN if norm < 1.0 else BAD)
        print(f"Load: {l1:.2f} / {l5:.2f} / {l15:.2f}  (1m / 5m / 15m) | size=13 {load_color}")
        print(f"  per core: {norm:.2f}  ·  {cores} cores | font=Menlo {CD}")
    if s["cpu_idle"] is not None:
        idle_color = OK if s["cpu_idle"] > 50 else (WARN if s["cpu_idle"] > 25 else BAD)
        print(f"CPU idle: {s['cpu_idle']:.0f}% | size=13 {idle_color}")
    print("---")

    # CCC (Claude Command Center) health
    ccc_color = {"ok": OK, "elevated": WARN, "high": BAD, "critical": BAD}.get(ccc_lvl, CD)
    print(f"CCC {level_emoji(ccc_lvl)} | size=13 {ccc_color}")
    if ccc_proc is None:
        print(f"  server: not running | font=Menlo {BAD}")
    else:
        cpu_color = OK if ccc_proc["cpu"] < 50 else (WARN if ccc_proc["cpu"] < 100 else BAD)
        print(
            f"  server: {ccc_proc['cpu']:.0f}% CPU · {fmt_mb(ccc_proc['rss_mb'])} · "
            f"up {fmt_etime(ccc_proc['etime_min'])} · PID {ccc_proc['pid']} | font=Menlo {cpu_color}"
        )
    if ccc_probe["ok"]:
        ms = ccc_probe["ms"]
        lat_color = OK if ms < 500 else (WARN if ms < 2000 else BAD)
        sess = ccc_probe["sessions"]
        sess_txt = f" · {sess} live session{'' if sess == 1 else 's'}" if sess is not None else ""
        print(f"  live-activity: {ms:.0f} ms{sess_txt} | font=Menlo {lat_color}")
    else:
        print(f"  live-activity: unreachable ({ccc_probe['error']}) | font=Menlo {BAD}")
    if ccc_errors is None:
        print(f"  errors: log not found | font=Menlo {CD}")
    else:
        err_color = OK if ccc_errors == 0 else (WARN if ccc_errors <= 5 else BAD)
        print(f"  errors (recent): {ccc_errors} | font=Menlo {err_color}")
    print("---")

    # Memory
    print(f"Memory | size=13 {C}")
    print(f"  {used} used · {avail} available | font=Menlo {CD}")
    print(f"  {comp} compressed | font=Menlo {CD}")
    swap = swap_usage()
    if swap and swap["total"]:
        used_pct = swap["used"] / swap["total"]
        sw_color = OK if used_pct < 0.5 else (WARN if used_pct < 0.85 else BAD)
        print(
            f"  swap: {fmt_mb(swap['used'])} / {fmt_mb(swap['total'])} "
            f"({used_pct * 100:.0f}%) | font=Menlo {sw_color}"
        )
    print(f"  {mem_summary} | font=Menlo {mem_color}")
    print("---")

    # Processes
    print(f"Processes: {s['procs']} · {s['threads']} threads | size=13 {C}")
    print(f"  claude CLI: {claude_cli}  ·  claude-index: {claude_index} | font=Menlo {CD}")
    print(f"  node: {node}  ·  npm: {npm} | font=Menlo {CD}")

    # Claude sessions — each anchors an MCP subtree; reap stale ones by the
    # whole tree, protect any that's driving an encode/transcode.
    sessions = claude_sessions(rows)
    if sessions:
        print("---")
        total_tree = sum(s["tree_rss"] for s in sessions)
        reapable = [s for s in sessions if s["reapable"]]
        reap_rss = sum(s["tree_rss"] for s in reapable)
        reap_tag = f" · {len(reapable)} reapable ({fmt_mb(reap_rss)})" if reapable else ""
        head_color = WARN if reapable else C
        print(f"Claude sessions: {len(sessions)} · {fmt_mb(total_tree)}{reap_tag} | size=13 {head_color}")
        for s in sessions[:8]:
            meta = (
                f"{fmt_etime(s['age_min'])} · {fmt_mb(s['tree_rss'])} · "
                f"{s['nprocs']} procs · {s['cwd']} · PID {s['pid']}"
            )
            kill_cmd = "kill -TERM " + " ".join(str(p) for p in s["tree_pids"])
            if s["interactive"]:
                print(f"  ⌨ {meta} · in use (terminal) | font=Menlo {CD}")
            elif s["busy"]:
                why = f"working: {s['worker']}" if s["worker"] else f"{s['tree_cpu']:.0f}% CPU"
                print(f"  ⚙ {meta} · {why} | font=Menlo {OK}")
            elif s["reapable"]:
                print(f"  🧹 {meta} · kill tree | font=Menlo {WARN} {sh_action(kill_cmd, refresh=True)}")
            else:
                print(f"  {meta} · idle (recent) · kill tree | font=Menlo {CD} {sh_action(kill_cmd, refresh=True)}")
        if len(reapable) > 1:
            kill_all = "kill -TERM " + " ".join(
                str(p) for s in reapable for p in s["tree_pids"]
            )
            print(f"  🧹 Reap all {len(reapable)} stale session trees ({fmt_mb(reap_rss)}) "
                  f"| font=Menlo {WARN} {sh_action(kill_all, refresh=True)}")

    # Should-be-zero alarms
    zombie_counts = {
        "cozempic": cozempic,
        "calendly-mcp-server": calendly,
    }
    alarms = [(name, zombie_counts[name]) for name, _pattern in ZOMBIE_PATTERNS if zombie_counts[name] > 0]
    if alarms:
        print("---")
        print(f"⚠️  Zombies detected | size=13 {BAD}")
        for name, n in alarms:
            print(f"  {name}: {n} running | font=Menlo {BAD}")

    if hogs:
        print("---")
        print(f"🔥 CPU hogs ({len(hogs)}) | size=13 {BAD}")
        for hog in hogs[:5]:
            line = (
                f"  {hog['cpu']:.0f}% · PID {hog['pid']} · {hog['label']} "
                f"· for {fmt_etime(hog['etime_min'])}"
            )
            if is_user_clearable(hog):
                kill_cmd = f"kill -TERM {hog['pid']}"
                line += f" · kill | font=Menlo {BAD} {sh_action(kill_cmd, refresh=True)}"
            else:
                line += f" | font=Menlo {BAD}"
            print(line)

    # Memory relief: ranked culprits plus safe, concrete next actions.
    print("---")
    print(f"Memory relief | size=13 {C}")
    suggestions = cleanup_suggestions(groups, alarms, hogs)
    if suggestions:
        for text, command, color in suggestions:
            if command:
                print(f"  {text} | font=Menlo {color} {sh_action(command, refresh=True)}")
            else:
                print(f"  {text} | font=Menlo {color}")
    else:
        print(f"  No obvious cleanup target right now | font=Menlo {OK}")

    print("  Open Activity Monitor | "
          "shell=/usr/bin/open param1=-a param2=\"Activity Monitor\" terminal=false")

    # Top memory groups (aggregated by app; kill actions live in the
    # suggestions above, so the per-PID list was redundant and was removed).
    print("  Top memory groups | font=Menlo {0}".format(CD))
    for group in groups[:5]:
        print(
            f"    {fmt_mb(group['rss_mb'])} · {group['count']} procs · {group['label']} "
            f"| font=Menlo {CD}"
        )

    print("---")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
