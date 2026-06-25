#!/bin/sh
# install.sh — install the sweettalker engine.
#
#   sweettalker.py -> ~/.local/bin/sweettalk    (the CLI)
#
# Re-runnable, and side-effect-light: it installs the binary, adds a source
# line to ~/.zshrc if absent, and stamps itself. It does NOT change your
# prompt or font — the prompt is rolled lazily on first shell (by
# sweettalker.zsh) and the look is painted onto st via OSC escapes on apply.
# The .zshrc self-install block re-runs this whenever the repo changes
# (nvwm-style).

set -eu

REPO="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

mkdir -p "$HOME/.local/bin"
cp -f "$REPO/sweettalker.py" "$HOME/.local/bin/sweettalk"
chmod 755 "$HOME/.local/bin/sweettalk"
echo "installed $HOME/.local/bin/sweettalk"

# zsh completion for `sweettalk` (and its look/<lever>/guess/learned aliases).
# The dir is on $fpath (see ~/.zshrc), so compinit picks it up next shell.
mkdir -p "$HOME/.local/share/zsh/site-functions"
cp -f "$REPO/completions/_sweettalk" "$HOME/.local/share/zsh/site-functions/_sweettalk"
chmod 644 "$HOME/.local/share/zsh/site-functions/_sweettalk"
echo "installed $HOME/.local/share/zsh/site-functions/_sweettalk"

ZSHRC="$HOME/.zshrc"
if ! grep -qF "sweettalker.zsh" "$ZSHRC" 2>/dev/null; then
    printf '\n# sweettalker — RL terminal-look selector\n[ -f %s/sweettalker.zsh ] && source %s/sweettalker.zsh\n' \
        "$REPO" "$REPO" >> "$ZSHRC"
    echo "added source line to $ZSHRC"
fi

# Stamp the install so the .zshrc block can tell when the repo has outpaced it.
touch "$HOME/.local/bin/.sweettalker-installed"
echo "done."
