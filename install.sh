#!/bin/bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="$HOME/Library/Application Support/xbar/plugins"
if [ ! -d "/Applications/xbar.app" ]; then echo "Install xbar first: brew install --cask xbar"; exit 1; fi
mkdir -p "$PLUGINS_DIR"
write_wrapper() {
    local fname="$1" py="$2"
    cat > "$PLUGINS_DIR/$fname" <<'WRAPPER'
#!/bin/bash
if defaults read -g AppleInterfaceStyle 2>/dev/null | grep -q Dark; then
    COLOR_SED='s/color=#[a-fA-F0-9]+,(#[a-fA-F0-9]+)/color=\1/g'
    FONT_SED='s/font=[^,| ]+,([^| ]+)/font=\1/g'
else
    COLOR_SED='s/(color=#[a-fA-F0-9]+),#[a-fA-F0-9]+/\1/g'
    FONT_SED='s/(font=[^,| ]+),[^| ]+/\1/g'
fi
exec "REPO_DIR_PLACEHOLDER/$py" "$@" | sed -E "$COLOR_SED; $FONT_SED"
WRAPPER
    sed -i '' "s|REPO_DIR_PLACEHOLDER/\$py|$REPO_DIR/$py|" "$PLUGINS_DIR/$fname"
    chmod +x "$PLUGINS_DIR/$fname"
}
write_wrapper claude-usage.5m.sh claude-usage.5m.py
write_wrapper mac-health.1m.sh mac-health.1m.py
chmod +x "$REPO_DIR"/*.py
echo "Installed. One-time setup: open a logged-in claude.ai tab in Chrome Beta and enable View -> Developer -> Allow JavaScript from Apple Events. Then: open -a xbar"
