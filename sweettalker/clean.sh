#!/bin/sh
# clean.sh — undo install.sh: remove sweettalker's installed binary, learned
# state, and its ~/.zshrc wiring (the SWEETTALKER_REPO var, the alias block, and
# the self-install block). The repo itself (~/sweettalker) is left untouched, so
# you can reinstall any time. (Looks are painted onto st live via OSC escapes,
# so there are no config files to remove.)

set -eu

ZSHRC="$HOME/.zshrc"

rm -f "$HOME/.local/bin/sweettalk" "$HOME/.local/bin/sweettalker"
rm -f "$HOME/.local/bin/.sweettalker-installed"
rm -rf "$HOME/.local/share/sweettalker"          # learned ratings + current.zsh
echo "removed sweettalker's binary and state"

# Strip sweettalker bits from ~/.zshrc as two contiguous ranges (the alias
# block and the self-install block) plus the SWEETTALKER_REPO line. Other
# content (nvwm, dotfiles alias, zsh enhancements) is left untouched.
if [ -f "$ZSHRC" ] && grep -qi 'sweettalk' "$ZSHRC"; then
    cp "$ZSHRC" "$ZSHRC.bak"
    awk '
        /^SWEETTALKER_REPO=/ { next }
        /^# sweettalker .* terminal looks/ { sa = 1 }
        sa { if ($0 ~ /^alias confide=/) sa = 0; next }
        /^# sweettalker: keep/ { sb = 1 }
        sb { if ($0 ~ /sweettalker\.zsh/) sb = 0; next }
        { print }
    ' "$ZSHRC" > "$ZSHRC.tmp" && mv "$ZSHRC.tmp" "$ZSHRC"
    echo "removed sweettalker block from $ZSHRC (backup at $ZSHRC.bak)"
fi

echo "done."
