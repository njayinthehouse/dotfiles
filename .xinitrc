#!/bin/sh
rm -f /tmp/nvim.sock
"$HOME/.local/bin/nvwm" 2>"$HOME/nvwm.log" &
sleep 0.4
exec alacritty --class nvwm_nvim -e nvim --listen /tmp/nvim.sock +terminal +startinsert
