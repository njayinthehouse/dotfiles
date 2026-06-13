NVWM_REPO="$HOME/nvwm"
SWEETTALKER_REPO="$HOME/sweettalker"

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
