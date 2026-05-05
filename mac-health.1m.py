#!/usr/bin/env python3
# <swiftbar.title>Mac Health</swiftbar.title>
# <swiftbar.author>amir</swiftbar.author>
# <swiftbar.desc>Load, memory pressure, process counts. Flags MCP/agent zombies.</swiftbar.desc>
# <swiftbar.dependencies>python3, top, pgrep</swiftbar.dependencies>
#
# Refresh: 1m (filename: mac-health.1m.py)
#
# Headline: emoji + 1m load avg, colored by pressure tier.
# Dropdown: load triplet, CPU idle, memory split, total processes,
# counts for things that tend to leak (cozempic, calendly-mcp-server,
# claude CLI sessions, node, npm). Anything in the "should-be-zero"
# row turns red if it appears.

import re
import subprocess

C  = "color=#1a1a1a,#ffffff"
CD = "color=#666666,#cfcfcf"
OK = "color=#0a7d20,#5dd66d"
WARN = "color=#b8860b,#e6c200"
BAD = "color=#c0392b,#ff7b7b"


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def parse_top():
    out = run(["top", "-l", "1", "-n", "0"])
    g = lambda pat, default=None: (m.groups() if (m := re.search(pat, out)) else default)
    procs = g(r"Processes:\s+(\d+) total.*?(\d+) threads") or (None, None)
    load  = g(r"Load Avg:\s+([\d.]+),\s+([\d.]+),\s+([\d.]+)") or (None, None, None)
    cpu   = g(r"CPU usage:.*?([\d.]+)% idle")
    mem   = g(r"PhysMem:\s+(\d+)([GM]) used \((\d+)M wired, (\d+)M compressor\),\s+(\d+)([GM]) unused")
    return {
        "procs":   int(procs[0]) if procs[0] else None,
        "threads": int(procs[1]) if procs[1] else None,
        "load":    [float(x) for x in load] if load[0] else [None, None, None],
        "cpu_idle": float(cpu[0]) if cpu else None,
        "mem":     mem,  # tuple or None
    }


def cpu_count():
    out = run(["sysctl", "-n", "hw.ncpu"]).strip()
    try:
        return int(out)
    except ValueError:
        return 1


def count(pgrep_args):
    out = run(["pgrep"] + pgrep_args)
    return len([l for l in out.splitlines() if l.strip()])


def fmt_mem(mem):
    if not mem:
        return "?", "?", "?"
    used_n, used_u, comp_m, _wired, free_n, free_u = mem
    used = f"{used_n} {used_u}B"
    free = f"{int(free_n)/1024:.1f} GB" if free_u == "M" and int(free_n) >= 1024 else f"{free_n} {free_u}B"
    comp = f"{int(comp_m)/1024:.1f} GB" if int(comp_m) >= 1024 else f"{comp_m} MB"
    return used, free, comp


def headline(load_1m, cores):
    """Color by load-per-core (the only honest way to read load avg)."""
    if load_1m is None:
        return "⚪ ?", None
    norm = load_1m / cores
    if norm < 0.7:   emoji = "🟢"
    elif norm < 1.0: emoji = "🟡"
    elif norm < 2.0: emoji = "🟠"
    else:            emoji = "🔴"
    return f"{emoji} {load_1m:.1f}", norm


def main():
    s = parse_top()
    l1, l5, l15 = s["load"]
    used, free, comp = fmt_mem(s["mem"])
    cores = cpu_count()

    # Counts
    claude_cli = count(["-x", "claude"])
    node       = count(["-x", "node"])
    npm        = count(["-f", r"^npm "])
    cozempic   = count(["-f", "cozempic"])
    calendly   = count(["-f", "calendly-mcp-server"])
    claude_index = count(["-f", "claude-index mcp"])

    # Headline (load / core, since absolute load is meaningless without core count)
    head, norm = headline(l1, cores)
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
    print(f"  {used} used · {free} free | font=Menlo {CD}")
    print(f"  {comp} compressed | font=Menlo {CD}")
    print("---")

    # Processes
    print(f"Processes: {s['procs']} · {s['threads']} threads | size=13 {C}")
    print(f"  claude CLI: {claude_cli}  ·  claude-index: {claude_index} | font=Menlo {CD}")
    print(f"  node: {node}  ·  npm: {npm} | font=Menlo {CD}")

    # Should-be-zero alarms
    alarms = []
    if cozempic > 0:
        alarms.append(("cozempic", cozempic))
    if calendly > 0:
        alarms.append(("calendly-mcp-server", calendly))
    if alarms:
        print("---")
        print(f"⚠️  Zombies detected | size=13 {BAD}")
        for name, n in alarms:
            print(f"  {name}: {n} running | font=Menlo {BAD}")

    print("---")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
