#!/bin/sh
# clean.sh — undo install.sh: remove nvwm's installed files and its ~/.zshrc
# self-install block. The repo itself (~/nvwm) is left untouched, so you can
# reinstall any time with install.sh.

set -eu

ZSHRC="$HOME/.zshrc"

rm -f "$HOME/.local/lib/wm.py"
rm -f "$HOME/.local/bin/nvwm"
rm -f "$HOME/.local/bin/pane"
rm -f "$HOME/.config/nvim/plugin/nvwm.lua"
rm -f "$HOME/.local/bin/.nvwm-installed"
echo "removed nvwm's installed files"

# Strip the NVWM_REPO line and the `# nvwm:` self-install block from ~/.zshrc.
if [ -f "$ZSHRC" ] && grep -q '^NVWM_REPO=\|^# nvwm:' "$ZSHRC"; then
    cp "$ZSHRC" "$ZSHRC.bak"
    awk '
        /^NVWM_REPO=/ { next }
        /^# nvwm:/ { skip = 1 }
        skip && /unset _nvwm_stamp/ { skip = 0; dropfi = 1; next }
        skip { next }
        dropfi { dropfi = 0; if ($0 ~ /^fi$/) next }
        { print }
    ' "$ZSHRC" > "$ZSHRC.tmp" && mv "$ZSHRC.tmp" "$ZSHRC"
    echo "removed nvwm block from $ZSHRC (backup at $ZSHRC.bak)"
fi

echo "done."
