#!/bin/sh
# clean.sh — undo install.sh: remove sweettalker's installed binary, learned
# state, the Alacritty look file + import, and its ~/.zshrc wiring (the
# SWEETTALKER_REPO var, the alias block, and the self-install block). The repo
# itself (~/sweettalker) is left untouched, so you can reinstall any time.

set -eu

ZSHRC="$HOME/.zshrc"

rm -f "$HOME/.local/bin/sweettalk" "$HOME/.local/bin/sweettalker"
rm -f "$HOME/.local/bin/.sweettalker-installed"
rm -rf "$HOME/.local/share/sweettalker"          # learned ratings + current.zsh
rm -f "$HOME/.config/alacritty/sweettalker.toml" \
      "$HOME/.config/alacritty/sweettalker-font.toml"
echo "removed sweettalker's binary, state, and look file"

# Strip the look import from alacritty.toml (leaves the rest intact).
ACFG="$HOME/.config/alacritty/alacritty.toml"
if [ -f "$ACFG" ] && grep -q 'sweettalker' "$ACFG"; then
    cp "$ACFG" "$ACFG.bak"
    grep -v 'sweettalker' "$ACFG" > "$ACFG.tmp" && mv "$ACFG.tmp" "$ACFG"
    echo "removed look import from $ACFG (backup at $ACFG.bak)"
fi

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
