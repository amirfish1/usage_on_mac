# Claude Usage — macOS Menu Bar

A pair of [xbar](https://xbarapp.com) plugins that sit in your macOS menu bar:

- **`claude-usage.5m.py`** — shows your real Anthropic Pro/Max weekly usage %, 5-hour session %, and a pace indicator (are you on track to stay under limits this week?). Pulls live data straight from the same endpoint that powers [claude.ai/settings/usage](https://claude.ai/settings/usage). Also shows Codex, Kimi (Kimi Code CLI), Google Antigravity (agy CLI), and Grok (xAI Grok CLI) usage — weekly %, 5h session %, pace, and extra-usage balance — sourced locally from each CLI's own data (Codex rollout logs; Kimi's OAuth token calling the same `api.kimi.com/coding/v1/usages` endpoint its `/usage` command uses; Antigravity's JSON output calling `agy --print /usage`; Grok's local credits log at `~/.grok/logs/unified.jsonl`). Features real brand Retina icons for each provider in the dropdown and authentic brand symbols in the menu bar.
- **`mac-health.1m.py`** — system load, memory pressure, zombie-process watchdog, and cleanup suggestions for heavy or leaked processes.

![screenshot showing menu bar with weekly % and dropdown with pace details]()

---

## What you see

**Menu bar headline:** `[Claude Icon] 29%·34  [Codex Icon] 99%·235  [Kimi Icon] 51%·184  [Gemini Icon] 43%·170  [Gemini Icon] 3P 0%·0  [Grok Icon] 8%·8`

- **Hybrid Coloring Scheme:**
  - **Weekly Usage %** is rendered in each provider's authentic brand color:
    - Claude: Terracotta (`#EB784B`)
    - Codex: OpenAI Emerald (`#10B981`)
    - Kimi: Sky Cyan (`#38BDF8`)
    - Antigravity / Gemini: Lavender Purple (`#A78BFA`)
    - Grok: Crisp Silver / Charcoal (`#F3F4F6` / `#1F2937`)
  - **Projected End-of-Week Pace %** is rendered in health status colors:
    - 🟢 **Green** (`#34C759`): On pace (projected ≤ 100%)
    - 🟡 **Yellow** (`#FFD60A`): Warning (projected 100% – 110%)
    - 🔴 **Red** (`#FF453A`): Burning fast / limit reached (projected > 110% or ≥ 100%)

**Dropdown (claude-usage with real Retina icons and colored status rows):**
```
Claude (Weekly limit all models)
  26% used · resets in 1d 6h
  on pace — projected 31% by week end
  5h session: 23% used · resets in 52m
  Extra usage (on): USD 13.35 of USD 120.00 (11.1%)
  Open Claude Web UI

Model split — this week
  Sonnet     42% of burn · cap —
  Haiku      27% of burn · cap —
  Opus       21% of burn · cap —
  Fable       9% of burn · 12% of its cap

Codex (prolite)
  99% used · resets in 4d 13h
  BURNING FAST — projected 250% by week end
  5h session: 4% used · resets in 3h 12m
  Open Codex Web UI

Kimi (Advanced)
  51% used · resets in 5d 13h
  BURNING FAST — projected 201% by week end
  5h session: 0% used · resets in 2h 45m
  Extra usage balance: USD 9.47
  Open Kimi Web UI

Antigravity
  Gemini Models
    37% used · resets in 5d 16h
    BURNING FAST — projected 161% by week end
    5h session: 26% used · resets in 3h 50m
  Claude/GPT Models
    0% used · resets in 6d 23h
    on pace — projected 0% by week end
    5h session: 0% used · resets in 4h 59m
  Open Antigravity Home

Grok (SuperGrok)
  8% used · resets in 16h 15m
  on pace — projected 21% by week end
  Extra usage balance: USD 10.00
  Open Grok Web UI

Active sessions: 3 · 1 need /compact
  🟠  68.3%    4m ago  ~/my-project
  🟢   8.1%   12m ago  ~/other-project

🌐 Open Web UIs
  Claude (claude.ai)
  Codex (chatgpt.com)
  Kimi (kimi.ai)
  Antigravity (antigravity.google)
  Grok (grok.com)
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
#    The wrappers exec the .py files from the repo and strip whichever half
#    (light/dark) of the comma-separated SwiftBar-style color/font hints
#    doesn't match your current system appearance, since xbar doesn't parse
#    the dual-value syntax itself.
mkdir -p "$HOME/Library/Application Support/xbar/plugins"
cat > "$HOME/Library/Application Support/xbar/plugins/claude-usage.5m.sh" <<'EOF'
#!/bin/bash
if defaults read -g AppleInterfaceStyle 2>/dev/null | grep -q Dark; then
    COLOR_SED='s/color=#[a-fA-F0-9]+,(#[a-fA-F0-9]+)/color=\1/g'
    FONT_SED='s/font=[^,| ]+,([^| ]+)/font=\1/g'
else
    COLOR_SED='s/(color=#[a-fA-F0-9]+),#[a-fA-F0-9]+/\1/g'
    FONT_SED='s/(font=[^,| ]+),[^| ]+/\1/g'
fi
exec "$HOME/dev/usage_on_mac/claude-usage.5m.py" "$@" | sed -E "$COLOR_SED; $FONT_SED"
EOF
cat > "$HOME/Library/Application Support/xbar/plugins/mac-health.1m.sh" <<'EOF'
#!/bin/bash
if defaults read -g AppleInterfaceStyle 2>/dev/null | grep -q Dark; then
    COLOR_SED='s/color=#[a-fA-F0-9]+,(#[a-fA-F0-9]+)/color=\1/g'
    FONT_SED='s/font=[^,| ]+,([^| ]+)/font=\1/g'
else
    COLOR_SED='s/(color=#[a-fA-F0-9]+),#[a-fA-F0-9]+/\1/g'
    FONT_SED='s/(font=[^,| ]+),[^| ]+/\1/g'
fi
exec "$HOME/dev/usage_on_mac/mac-health.1m.py" "$@" | sed -E "$COLOR_SED; $FONT_SED"
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
| `_codex_lib.py` | Helper — Codex weekly/5h usage from local `~/.codex` rollout logs |
| `_kimi_lib.py` | Helper — Kimi (Kimi Code CLI) weekly/5h/extra usage via `~/.kimi-code` OAuth token + `api.kimi.com/coding/v1/usages` |
| `_antigravity_lib.py` | Helper — Antigravity weekly/5h usage via `agy --print /usage --output-format json` |
| `_grok_lib.py` | Helper — Grok (xAI Grok CLI) weekly usage via local `~/.grok/logs/unified.jsonl` credits config |
| `_icons_lib.py` | Helper — provides real brand Retina icons (base64 PNG) for Claude, Codex, Kimi, Antigravity, and Grok |
| `icons/` | Directory containing light/dark and 32x32 Retina PNG brand icons |
| `ccc-context-fill.py` | CLI tool — same context-fill data as a table or JSON |
| `mac-health.1m.py` | xbar plugin — system load, memory, zombie process watchdog, cleanup suggestions |

---

## Mac health cleanup

When memory gets tight, `mac-health.1m.py` now ranks the biggest memory groups and processes, then suggests the first cleanup moves to try. Known leaked helpers such as `cozempic` and `calendly-mcp-server` get one-click `pkill -TERM` actions; general high-memory apps/processes stay advisory, with click-to-copy `kill -TERM <pid>` commands for targeted cleanup.

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
