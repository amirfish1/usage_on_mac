# Claude Usage — macOS Menu Bar

A pair of [xbar](https://xbarapp.com) plugins that sit in your macOS menu bar:

- **`claude-usage.5m.py`** — shows your real Anthropic Pro/Max weekly usage %, 5-hour session %, and a pace indicator (are you on track to stay under limits this week?). Pulls live data straight from the same endpoint that powers [claude.ai/settings/usage](https://claude.ai/settings/usage).
- **`mac-health.1m.py`** — system load, memory pressure, and a zombie-process watchdog (flags leaked MCP servers, orphaned `claude` CLI sessions, etc.).

![screenshot showing menu bar with weekly % and dropdown with pace details]()

---

## What you see

**Menu bar headline:** `🟢 8% · 72% proj`

- `8%` = weekly usage consumed so far
- `72% proj` = projected usage by end of week at your current pace (green / yellow / orange / red)

**Dropdown (claude-usage):**
```
Weekly limit (all models)
  8% used · resets in 5d 3h
  Used 8% · expected 11% · Δ -3pp
  on pace — projected 72% by week end
  Worked 9.5h of 91h (7:00–20:00 daily, 7d)

Weekly Sonnet only
  2% used · resets in 5d 3h

Current 5h session
  7% used · resets in 3h 12m

Extra usage (on)
  USD 72.62 of USD 120.00 (60.5%)

Active sessions: 3 · 1 need /compact
  🟠  68.3%    4m ago  claude-sonnet-4-6   my-project
  🟢   8.1%   12m ago  claude-opus-4-7     other-project
```

---

## Requirements

- macOS 12+
- [xbar](https://xbarapp.com) — `brew install --cask xbar`
- **Google Chrome Beta** with a `claude.ai` tab open and logged in
- Python 3 (comes with macOS)

> **Why Chrome Beta?** The plugin reuses your existing logged-in browser session to call the usage API — no tokens, no credentials stored anywhere. Chrome Beta is simply what I run. If you use regular Chrome, change `"Google Chrome Beta"` → `"Google Chrome"` in `fetch-usage.applescript`.

---

## Install

```bash
# 1. Clone
git clone https://github.com/amirfish1/usage_on_mac.git ~/dev/usage_on_mac

# 2. Install xbar (if you haven't)
brew install --cask xbar

# 3. Drop wrapper scripts into xbar's plugin directory.
#    The wrappers exec the .py files from the repo and strip the
#    SwiftBar-style dark/light color and font hints that xbar doesn't parse.
mkdir -p "$HOME/Library/Application Support/xbar/plugins"
cat > "$HOME/Library/Application Support/xbar/plugins/claude-usage.5m.sh" <<'EOF'
#!/bin/bash
exec "$HOME/dev/usage_on_mac/claude-usage.5m.py" "$@" | sed -E 's/(color=#[a-fA-F0-9]+),#[a-fA-F0-9]+/\1/g; s/(font=[^,| ]+),[^| ]+/\1/g'
EOF
cat > "$HOME/Library/Application Support/xbar/plugins/mac-health.1m.sh" <<'EOF'
#!/bin/bash
exec "$HOME/dev/usage_on_mac/mac-health.1m.py" "$@" | sed -E 's/(color=#[a-fA-F0-9]+),#[a-fA-F0-9]+/\1/g; s/(font=[^,| ]+),[^| ]+/\1/g'
EOF
chmod +x "$HOME/Library/Application Support/xbar/plugins/"*.sh

# 4. Enable JavaScript from Apple Events in Chrome Beta
#    Chrome Beta → View → Developer → Allow JavaScript from Apple Events ✓
#    (one-time, survives restarts)

# 5. Open xbar
open -a xbar
```

xbar will immediately show the plugins in your menu bar. `claude-usage` refreshes every 5 minutes; `mac-health` every 1 minute.

To launch xbar automatically at login: **System Settings → General → Login Items → +** and add `/Applications/xbar.app`.

### Switch from Chrome Beta to regular Chrome

Open `fetch-usage.applescript` and change both occurrences of `"Google Chrome Beta"` to `"Google Chrome"`.

---

## How it works

`fetch-usage.applescript` uses AppleScript's "JavaScript from Apple Events" feature to ask an already-open, already-logged-in Chrome tab to run:

```js
fetch('/api/organizations/<your-org-id>/usage', {credentials: 'include'})
```

This is the same API call that `claude.ai/settings/usage` makes in your browser. The plugin:
- Finds your org ID dynamically (no hardcoding)
- Has no access to your cookies or credentials — it just asks Chrome to make the call in-tab
- Caches the last successful result so the menu still shows something if Chrome is closed

**Memory footprint:** ~25MB peak (Python + one subprocess). Runs every 5 minutes, not continuously.

---

## Pace indicator

The weekly % alone doesn't tell you if you're over- or under-using. The pace indicator compares actual usage to expected usage given how many "work hours" have elapsed in the week.

Default assumption: you work 7am–8pm every day (91 h/week). Edit these at the top of `claude-usage.5m.py`:

```python
WORK_START_HOUR = 7
WORK_END_HOUR = 20
WORK_DAYS_PER_WEEK = 7
```

---

## Files

| File | Purpose |
|---|---|
| `claude-usage.5m.py` | Main xbar plugin — usage %, pace, active sessions |
| `fetch-usage.applescript` | Fetches the usage JSON from an open Chrome/claude.ai tab |
| `_session_lib.py` | Helper — reads local `~/.claude` transcripts to show context-fill per session |
| `ccc-context-fill.py` | CLI tool — same context-fill data as a table or JSON |
| `mac-health.1m.py` | xbar plugin — system load, memory, zombie process watchdog |

---

## CLI: session context fill

```bash
# Table view — sessions active in the last hour
python3 ccc-context-fill.py --table

# JSON (for piping into other tools)
python3 ccc-context-fill.py --pretty

# Only sessions that should /compact soon
python3 ccc-context-fill.py --table --only-warning
```

---

## Troubleshooting

**Plugin shows `⚠️` or "no claude.ai tab open"**
- Make sure a `claude.ai` tab is open in Chrome Beta
- Check that "Allow JavaScript from Apple Events" is enabled (Chrome Beta → View → Developer)

**"Allow JavaScript from Apple Events" is grayed out**
- Quit and reopen Chrome Beta; the option sometimes needs a fresh start to appear

**Using regular Chrome instead of Chrome Beta**
- Edit `fetch-usage.applescript` and replace `"Google Chrome Beta"` with `"Google Chrome"`

**xbar isn't showing the plugins**
- Verify the wrappers exist and are executable: `ls -l ~/Library/Application\ Support/xbar/plugins/`
- Make sure the underlying `.py` files are executable: `chmod +x ~/dev/usage_on_mac/*.py`
- Open xbar's preferences and confirm it's pointed at `~/Library/Application Support/xbar/plugins/` (the default)

---

## License

MIT
