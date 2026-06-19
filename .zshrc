NVWM_REPO="$HOME/nvwm"
SWEETTALKER_REPO="$HOME/sweettalker"
ST_REPO="$HOME/st"
LLVM_REPO="$HOME/llvm-project"

export PATH="$HOME/.local/bin:$PATH"

# from-source LLVM/clang toolchain (our default compiler; gcc dropped). The
# runtime libs (libc++/libunwind) install to a per-target subdir, so the loader
# needs it on its path for clang-built C++ binaries to run.
export PATH="$HOME/.local/llvm/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/llvm/lib/x86_64-unknown-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Private, non-dotfile aliases (kept out of the `dot` repo).
[[ -f ~/.zsh_aliases ]] && source ~/.zsh_aliases

# Common aliases
alias l='ls'
alias la='ls -a'
alias ll='ls -l'
alias lla='ls -l -a'
alias zshconf='nvim $HOME/.zsh'
alias nvimconf='nvim $HOME/.conf/nvim/init.lua'
alias vim='nvim'
alias grep='rg'
alias firefox='firefox >$HOME/.logs/firefox.log 2>&1 &'

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
