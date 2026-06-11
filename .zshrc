NVWM_REPO="$HOME/nvwm"

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
