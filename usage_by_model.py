#!/usr/bin/env python3
"""Per-model token burn from local Claude Code transcripts.

Sums assistant-message usage by model across ~/.claude/projects/*/*.jsonl and
weights it into a single "burn" number per model family:

    burn = input + 5*output + 1.25*cache_write + 0.1*cache_read

The weights approximate relative pricing, so each family's share of total burn
tracks its share of the weekly rate limit even where the settings/usage API
has no per-model bucket.

Reusable: `usage_by_model(since_local)` returns {family: {tokens..., burn,
share}}. CLI: `./usage_by_model.py [--days 7] [--since ISO] [--json]`.
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR")
                    or Path.home() / ".claude" / "projects")
CACHE_FILE = Path(os.environ.get("CLAUDE_USAGE_MODEL_CACHE")
                  or Path.home() / ".cache" / "claude-usage-model-tokens.json")

W_INPUT = 1.0
W_OUTPUT = 5.0
W_CACHE_WRITE = 1.25
W_CACHE_READ = 0.1

_CACHE_VERSION = 2

FAMILY_ORDER = ["fable", "opus", "sonnet", "haiku", "other"]


def model_family(model):
    """Map a model id (e.g. 'claude-fable-5') to a family bucket."""
    m = (model or "").lower()
    for family in ("fable", "opus", "sonnet", "haiku"):
        if family in m:
            return family
    return "other"


def weighted_burn(input_tokens, output_tokens, cache_write, cache_read):
    return (
        W_INPUT * input_tokens
        + W_OUTPUT * output_tokens
        + W_CACHE_WRITE * cache_write
        + W_CACHE_READ * cache_read
    )


def _parse_file(path, cutoff_utc_iso):
    """Extract [msg_id, model, in, out, cache_write, cache_read] entries from
    one transcript. Only assistant records at/after the cutoff count."""
    entries = []
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
                ts = rec.get("timestamp")
                if not ts or ts < cutoff_utc_iso:
                    continue
                msg = rec.get("message") or {}
                msg_id = msg.get("id")
                usage = msg.get("usage") or {}
                if not msg_id or not usage:
                    continue
                inp = usage.get("input_tokens") or 0
                out = usage.get("output_tokens") or 0
                cw = usage.get("cache_creation_input_tokens") or 0
                cr = usage.get("cache_read_input_tokens") or 0
                if inp + out + cw + cr <= 0:
                    continue
                entries.append([msg_id, msg.get("model") or "", inp, out, cw, cr])
    except Exception:
        pass
    return entries


def usage_by_model(since_local, projects_dir=None, cache_file=None):
    """Aggregate per-family usage since `since_local` (aware datetime).

    Returns {family: {"input", "output", "cache_write", "cache_read",
    "burn", "share"}} for families with any burn, plus "_total" with the
    summed burn. Shares are fractions of total burn in [0, 1].

    A message id counts once (streaming rewrites the same message across
    lines); the occurrence with the largest burn wins.
    """
    projects_dir = Path(projects_dir) if projects_dir else PROJECTS_DIR
    cache_file = Path(cache_file) if cache_file else CACHE_FILE
    cutoff_utc_iso = since_local.astimezone(timezone.utc).isoformat()

    try:
        cache = json.loads(cache_file.read_text())
    except Exception:
        cache = {}
    if cache.get("_v") != _CACHE_VERSION or cache.get("_since") != cutoff_utc_iso:
        cache = {}
    new_cache = {"_v": _CACHE_VERSION, "_since": cutoff_utc_iso}

    # Files untouched since a day before the window can't contain records in it.
    cutoff_mtime = (since_local - timedelta(days=1)).timestamp()
    best = {}  # msg_id -> [burn, family, inp, out, cw, cr]
    for path in glob.glob(str(projects_dir / "*" / "*.jsonl")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < cutoff_mtime:
            continue
        sig = f"{int(st.st_mtime)}:{st.st_size}"
        prev = cache.get(path)
        if prev and prev.get("sig") == sig:
            entries = prev["entries"]
        else:
            entries = _parse_file(path, cutoff_utc_iso)
        new_cache[path] = {"sig": sig, "entries": entries}
        for msg_id, model, inp, out, cw, cr in entries:
            burn = weighted_burn(inp, out, cw, cr)
            cur = best.get(msg_id)
            if cur is None or burn > cur[0]:
                best[msg_id] = [burn, model_family(model), inp, out, cw, cr]

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(new_cache))
    except Exception:
        pass

    fams = {}
    for burn, family, inp, out, cw, cr in best.values():
        f = fams.setdefault(family, {
            "input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
            "burn": 0.0,
        })
        f["input"] += inp
        f["output"] += out
        f["cache_write"] += cw
        f["cache_read"] += cr
        f["burn"] += burn

    total = sum(f["burn"] for f in fams.values())
    for f in fams.values():
        f["share"] = (f["burn"] / total) if total else 0.0
    fams["_total"] = {"burn": total}
    return fams


def format_split(fams):
    """One line per family, largest burn first: 'fable  61.2%  burn=1.2B'."""
    lines = []
    families = [k for k in fams if k != "_total"]
    families.sort(key=lambda k: -fams[k]["burn"])
    for name in families:
        f = fams[name]
        lines.append(
            f"{name:<7} {f['share'] * 100:5.1f}%  burn={f['burn'] / 1e6:,.1f}M "
            f"(in={f['input']:,} out={f['output']:,} "
            f"cw={f['cache_write']:,} cr={f['cache_read']:,})"
        )
    if not lines:
        lines.append("no usage found in window")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7.0,
                    help="window size in days back from now (default 7)")
    ap.add_argument("--since", help="ISO datetime window start (overrides --days)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.astimezone()
    else:
        since = datetime.now().astimezone() - timedelta(days=args.days)

    fams = usage_by_model(since)
    if args.json:
        print(json.dumps({"since": since.isoformat(), "families": fams}, indent=1))
    else:
        print(f"Usage by model since {since.isoformat()}")
        for line in format_split(fams):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
