#!/bin/sh
# Roll one fresh terminal look per startx session (if autoroll is on); shells
# then just apply it via `sweettalk startup`, so the whole session shares one look.
"$HOME/.local/bin/sweettalk" session >/dev/null 2>&1

rm -f /tmp/nvim.sock
"$HOME/.local/bin/nvwm" 2>"$HOME/nvwm.log" &
sleep 0.4
exec alacritty --class nvwm_nvim -e nvim --listen /tmp/nvim.sock +terminal +startinsert
