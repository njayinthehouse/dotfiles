# sweettalker — terminal "look" selector. Source from ~/.zshrc.
#
# On shell start one engine call applies the session's current look; then the
# resulting prompt is loaded. Rolling a fresh look is the startx session's job
# (`sweettalk session`, from ~/.xinitrc, when autoroll is on), so opening a new
# pane never changes the look out from under you. The look / prompt /
# font / size / foreground / background / palette / confide commands are
# defined as aliases in ~/.zshrc.

SWEETTALKER_DATA="${SWEETTALKER_DATA:-$HOME/.local/share/sweettalker}"
SWEETTALKER_CURRENT="$SWEETTALKER_DATA/current.zsh"

# Git segment for prompts that reference ${vcs_info_msg_0_}.
autoload -Uz vcs_info
zstyle ':vcs_info:*' enable git
zstyle ':vcs_info:git:*' formats       ' %F{magenta}(%b)%f'
zstyle ':vcs_info:git:*' actionformats ' %F{magenta}(%b|%a)%f'
setopt prompt_subst
(( ${precmd_functions[(I)vcs_info]} )) || precmd_functions+=(vcs_info)

command -v sweettalk >/dev/null && sweettalk startup
[[ -f $SWEETTALKER_CURRENT ]] && source $SWEETTALKER_CURRENT
