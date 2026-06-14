NVWM_REPO="$HOME/nvwm"
SWEETTALKER_REPO="$HOME/sweettalker"
ST_REPO="$HOME/st"

export PATH="$HOME/.local/bin:$PATH"

# Common aliases
alias l='ls'
alias la='ls -a'
alias ll='ls -l'
alias lla='ls -l -a'
alias zshconf='nvim ~/.zsh'
alias nvimconf='nvim ~/.conf/nvim/init.lua'
alias vim='nvim'
alias grep='rg'

# sweettalker — roll/rate terminal looks (fg/bg are job-control builtins, so
# the colour levers are 'foreground'/'background')
alias look='sweettalk look'
alias prompt='sweettalk prompt'
alias font='sweettalk font'
alias size='sweettalk size'
alias foreground='sweettalk foreground'
alias background='sweettalk background'
alias palette='sweettalk palette'
alias confide='sweettalk confide'
alias learned='sweettalk learned'

# Version control for dotfiles
alias dot='git --git-dir=$HOME/.dotfiles --work-tree=$HOME'

# wifi — list networks / connect / disconnect (NetworkManager). A function, not
# an alias, so it can dispatch on the subcommand. The wifi device is discovered
# rather than hardcoded.
wifi() {
  local dev radio conn
  dev=$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="wifi"{print $1; exit}')
  case "$1" in
    ""|status)      # show radio state and what we're connected to
      radio=$(nmcli radio wifi)
      if [[ $radio != enabled ]]; then
        echo "wifi: off"
      else
        conn=$(nmcli -t -f GENERAL.CONNECTION device show "$dev" 2>/dev/null | cut -d: -f2)
        if [[ -n $conn && $conn != "--" ]]; then
          echo "wifi: on — connected to $conn"
        else
          echo "wifi: on — not connected"
        fi
      fi ;;
    list|ls)        nmcli device wifi list ;;
    connect|c)
      shift
      local always=0 cargs=() a
      for a in "$@"; do [[ $a == --always ]] && always=1 || cargs+=("$a"); done
      nmcli --ask device wifi connect "${cargs[@]}"
      if (( always )); then               # prefer + auto-connect this SSID
        nmcli connection modify "${cargs[1]}" connection.autoconnect yes \
              connection.autoconnect-priority 100 2>/dev/null \
          && echo "wifi: ${cargs[1]} set to always auto-connect when available"
      fi ;;
    disconnect|d)   nmcli device disconnect "$dev" ;;
    on)             nmcli radio wifi on ;;
    off)            nmcli radio wifi off ;;
    help|-h|--help) print "usage: wifi [status | list | connect <ssid> [--always] | disconnect | on | off | help]" ;;
    *) print -u2 "wifi: unknown command '$1' (try: wifi help)" ;;
  esac
}

# bluetooth — status / list / scan / connect / disconnect / on / off (bluez).
# Mirrors `wifi`; drives bluetoothctl + rfkill. connect/disconnect/pair take a
# MAC, or a case-insensitive name substring resolved from the known devices.
bt() {
  local mac
  case "$1" in
    ""|status)
      if rfkill list bluetooth 2>/dev/null | grep -q 'Soft blocked: yes'; then
        echo "bluetooth: off (rfkill blocked)"
      elif bluetoothctl show 2>/dev/null | grep -q 'Powered: yes'; then
        echo "bluetooth: on"
        local con
        con=$(bluetoothctl devices Connected 2>/dev/null \
              | sed 's/^Device [0-9A-F:]* /  connected: /')
        [[ -n $con ]] && echo "$con" || echo "  (nothing connected)"
      else
        echo "bluetooth: powered off"
      fi ;;
    list|ls)   bluetoothctl devices ;;
    paired)    bluetoothctl devices Paired ;;
    scan)      echo "scanning ${2:-10}s…"
               bluetoothctl --timeout "${2:-10}" scan on >/dev/null 2>&1
               bluetoothctl devices ;;
    connect|c)
      shift
      local always=0 cargs=() a
      for a in "$@"; do [[ $a == --always ]] && always=1 || cargs+=("$a"); done
      mac=$(_bt_resolve "${cargs[@]}") || { print -u2 "bt: no device matching '${cargs[*]}'"; return 1; }
      bluetoothctl connect "$mac"
      (( always )) && bluetoothctl trust "$mac" >/dev/null \
        && echo "bt: trusted $mac — will auto-reconnect when available" ;;
    disconnect|d)
      shift
      if [[ -n $1 ]]; then
        mac=$(_bt_resolve "$@") || { print -u2 "bt: no device matching '$*'"; return 1; }
        bluetoothctl disconnect "$mac"
      else
        bluetoothctl disconnect            # no arg → drop all connections
      fi ;;
    pair|p)
      shift
      mac=$(_bt_resolve "$@") || { print -u2 "bt: no device matching '$*'"; return 1; }
      bluetoothctl pair "$mac" ;;
    on)        rfkill unblock bluetooth 2>/dev/null; bluetoothctl power on ;;
    off)       bluetoothctl power off ;;
    help|-h|--help) print "usage: bt [status | list | paired | scan [secs] | connect <name|mac> [--always] | disconnect [name|mac] | pair <name|mac> | on | off]" ;;
    *) print -u2 "bt: unknown command '$1' (try: bt help)" ;;
  esac
}

# resolve a MAC directly, or the first known device whose name matches the args
_bt_resolve() {
  if [[ $1 == [0-9A-Fa-f][0-9A-Fa-f]:* ]]; then
    print -r -- "$1"
  else
    bluetoothctl devices 2>/dev/null | grep -i -- "$*" | head -1 | awk '{print $2}' | grep .
  fi
}

# nvwm: re-run install.sh whenever the repo has changed since the last install
# (handles a fresh checkout, missing files, or edited sources). The stamp file
# is refreshed by install.sh, so this only fires on real changes.
if [[ -d ~/nvwm ]]; then
  _nvwm_stamp=~/.local/bin/.nvwm-installed
  if [[ ! -e $_nvwm_stamp ]] || \
     [[ -n $(find $NVWM_REPO -type f -not -path '*/__pycache__/*' -newer $_nvwm_stamp -print -quit 2>/dev/null) ]]; then
    sh $NVWM_REPO/install.sh >/dev/null && echo "nvwm: symlinks reinstalled"
  fi
  unset _nvwm_stamp
fi

# sweettalker: keep ~/.local/bin/sweettalker in sync with the repo (same
# self-install pattern as nvwm), then load the prompt/font bandits.
if [[ -d $SWEETTALKER_REPO ]]; then
  _st_stamp=~/.local/bin/.sweettalker-installed
  if [[ ! -e $_st_stamp ]] || \
     [[ -n $(find $SWEETTALKER_REPO -type f -not -path '*/__pycache__/*' -newer $_st_stamp -print -quit 2>/dev/null) ]]; then
    sh $SWEETTALKER_REPO/install.sh >/dev/null && echo "sweettalker: reinstalled"
  fi
  unset _st_stamp
fi
[[ -f $SWEETTALKER_REPO/sweettalker.zsh ]] && source $SWEETTALKER_REPO/sweettalker.zsh

# st: rebuild + reinstall when the repo changes (same stamp pattern as nvwm).
# Guarded on `make` so it stays quiet until the build toolchain is installed.
if [[ -d $ST_REPO ]] && command -v make >/dev/null; then
  _stbin_stamp=~/.local/bin/.st-installed
  if [[ ! -e $_stbin_stamp ]] || \
     [[ -n $(find $ST_REPO -type f -newer $_stbin_stamp -print -quit 2>/dev/null) ]]; then
    sh $ST_REPO/install.sh >/dev/null && echo "st: rebuilt + installed"
  fi
  unset _stbin_stamp
fi

# ── zsh enhancements: the useful parts of oh-my-zsh, no framework ───────────
# Install: sudo pacman -S --needed zsh-autosuggestions zsh-syntax-highlighting \
#          zsh-history-substring-search zsh-completions

# History
HISTFILE=~/.zsh_history
HISTSIZE=10000
SAVEHIST=10000
setopt SHARE_HISTORY HIST_IGNORE_DUPS HIST_IGNORE_SPACE HIST_VERIFY

# Completion: case-insensitive, menu selection, colourised. zsh-completions
# drops its functions in $fpath automatically, so compinit just picks them up.
autoload -Uz compinit && compinit
zstyle ':completion:*' menu select
zstyle ':completion:*' matcher-list 'm:{a-zA-Z}={A-Za-z}'
zstyle ':completion:*' list-colors "${(s.:.)LS_COLORS}"
zstyle ':completion:*' group-name ''

# Autosuggestions — fish-style ghost text from history (→ / End accepts).
[[ -f /usr/share/zsh/plugins/zsh-autosuggestions/zsh-autosuggestions.zsh ]] && \
  source /usr/share/zsh/plugins/zsh-autosuggestions/zsh-autosuggestions.zsh

# Syntax highlighting — must be sourced late (after compinit + any widgets).
[[ -f /usr/share/zsh/plugins/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh ]] && \
  source /usr/share/zsh/plugins/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh

# History substring search — after syntax-highlighting; bind ↑/↓ (both cursor
# modes) to search history by the prefix you've typed.
if [[ -f /usr/share/zsh/plugins/zsh-history-substring-search/zsh-history-substring-search.zsh ]]; then
  source /usr/share/zsh/plugins/zsh-history-substring-search/zsh-history-substring-search.zsh
  bindkey '^[[A' history-substring-search-up;   bindkey '^[OA' history-substring-search-up
  bindkey '^[[B' history-substring-search-down; bindkey '^[OB' history-substring-search-down
fi
