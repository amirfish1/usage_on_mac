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

import os
import re
import shlex
import subprocess

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


def headline(load_1m, cores, mem_level):
    """Color by load-per-core (the only honest way to read load avg)."""
    if load_1m is None:
        return f"{level_emoji(mem_level)} mem" if level_rank(mem_level) else "⚪ ?", None
    norm = load_1m / cores
    overall = mem_level if level_rank(mem_level) > level_rank(load_level(norm)) else load_level(norm)
    emoji = level_emoji(overall)
    return f"{emoji} {load_1m:.1f}", norm


def process_rows():
    out = run([PS, "-axo", "pid=,ppid=,rss=,pcpu=,command="])
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_mb = int(parts[2]) / 1024
            cpu = float(parts[3])
        except ValueError:
            continue
        cmd = parts[4]
        if pid == os.getpid() or "mac-health.1m.py" in cmd:
            continue
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "rss_mb": rss_mb,
            "cpu": cpu,
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


def cleanup_suggestions(groups, alarms):
    suggestions = []

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

    # Counts
    claude_cli = count(["-x", "claude"])
    node       = count(["-x", "node"])
    npm        = count(["-f", r"^npm "])
    cozempic   = count(["-f", "cozempic"])
    calendly   = count(["-f", "calendly-mcp-server"])
    claude_index = count(["-f", "claude-index mcp"])

    # Headline (load / core, since absolute load is meaningless without core count)
    head, norm = headline(l1, cores, mem_level)
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

    # Memory
    print(f"Memory | size=13 {C}")
    print(f"  {used} used · {avail} available | font=Menlo {CD}")
    print(f"  {comp} compressed | font=Menlo {CD}")
    print(f"  {mem_summary} | font=Menlo {mem_color}")
    print("---")

    # Processes
    print(f"Processes: {s['procs']} · {s['threads']} threads | size=13 {C}")
    print(f"  claude CLI: {claude_cli}  ·  claude-index: {claude_index} | font=Menlo {CD}")
    print(f"  node: {node}  ·  npm: {npm} | font=Menlo {CD}")

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

    # Memory relief: ranked culprits plus safe, concrete next actions.
    print("---")
    print(f"Memory relief | size=13 {C}")
    suggestions = cleanup_suggestions(groups, alarms)
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

    print("  Top memory groups | font=Menlo {0}".format(CD))
    for group in groups[:5]:
        print(
            f"    {fmt_mb(group['rss_mb'])} · {group['count']} procs · {group['label']} "
            f"| font=Menlo {CD}"
        )

    print("  Top memory processes | font=Menlo {0}".format(CD))
    for row in top_memory_processes(rows):
        line = f"    {fmt_mb(row['rss_mb'])} · PID {row['pid']} · {row['label']}"
        if is_user_clearable(row):
            kill_cmd = f"kill -TERM {row['pid']}"
            line += f" · copy kill cmd | font=Menlo {CD} {copy_action(kill_cmd)}"
        else:
            line += f" | font=Menlo {CD}"
        print(line)

    print("---")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
