#!/bin/sh
# install.sh — copy nvwm's components into the locations the system expects.
#
#   wm.py     -> ~/.local/lib/wm.py            (library, imported by both)
#   nvwm.py   -> ~/.local/bin/nvwm             (the WM, run by .xinitrc)
#   pane.py   -> ~/.local/bin/pane             (the pane CLI)
#   sesh.py   -> ~/.local/bin/sesh             (the session CLI)
#   nvwm.lua  -> ~/.config/nvim/plugin/nvwm.lua (auto-sourced plugin)
#
# Re-runnable: destinations are overwritten from the repo, which is the single
# source of truth. .zshrc re-runs this automatically when the repo changes.

set -eu

REPO="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

install_file() {  # install_file <file-in-repo> <destination> <mode>
    src="$REPO/$1"
    dest="$2"
    mode="$3"
    mkdir -p -- "$(dirname -- "$dest")"
    cp -f -- "$src" "$dest"
    chmod "$mode" -- "$dest"
    echo "installed $dest"
}

install_file wm.py    "$HOME/.local/lib/wm.py"               644
install_file nvwm.py  "$HOME/.local/bin/nvwm"                755
install_file pane.py  "$HOME/.local/bin/pane"                755
install_file sesh.py  "$HOME/.local/bin/sesh"                755
install_file nvwm.lua "$HOME/.config/nvim/plugin/nvwm.lua"   644

# zsh completions for the pane/sesh CLIs. The dir is on $fpath (see ~/.zshrc),
# so compinit picks these up on the next shell.
install_file completions/_pane "$HOME/.local/share/zsh/site-functions/_pane"   644
install_file completions/_sesh "$HOME/.local/share/zsh/site-functions/_sesh"   644

# Stamp the install so .zshrc can tell when the repo has outpaced it and
# re-run us automatically.
touch -- "$HOME/.local/bin/.nvwm-installed"

echo "done. restart X to pick up the new WM, or :NvwmRefresh for a resync."
