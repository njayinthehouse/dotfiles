#!/bin/sh
rm -f /tmp/nvim.sock
nvwm 2>"$HOME/.nvwm.log" &
sleep 0.4
exec alacritty --class nvwm_nvim
