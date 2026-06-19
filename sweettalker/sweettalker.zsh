# sweettalker — RL terminal-"look" selector. Source from ~/.zshrc.
#
# On shell start one engine call applies the session's current look, then the
# prompt is loaded. Rolling a fresh look is the startx session's job
# (`sweettalk session`, from ~/.xinitrc, when autoroll is on).
#
# Live prompt updates: colours/font are painted onto the shared st via OSC
# escapes, so every pane recolours at once. The *prompt* is per-shell zsh state,
# so a roll can't reach into running shells directly — instead the engine writes
# the new prompt to current.zsh and a precmd hook re-sources it whenever its
# mtime changes. So any roll (in any pane) updates the prompt everywhere on the
# next prompt draw, mirroring how recolouring st hits every pane.

SWEETTALKER_DATA="${SWEETTALKER_DATA:-$HOME/.local/share/sweettalker}"
SWEETTALKER_CURRENT="$SWEETTALKER_DATA/current.zsh"

# Git segment for prompts that include it (${vcs_info_msg_0_}).
autoload -Uz vcs_info
zstyle ':vcs_info:*' enable git
zstyle ':vcs_info:git:*' formats       ' %F{5}(%b)%f'
zstyle ':vcs_info:git:*' actionformats ' %F{5}(%b|%a)%f'
setopt prompt_subst
(( ${precmd_functions[(I)vcs_info]} )) || precmd_functions+=(vcs_info)

# Re-source current.zsh when it changes, so rolled prompts apply live everywhere.
zmodload -F zsh/stat b:zstat 2>/dev/null
_sweettalk_prompt_reload() {
  [[ -f $SWEETTALKER_CURRENT ]] || return
  local -a s
  zstat -A s +mtime $SWEETTALKER_CURRENT 2>/dev/null || return
  if [[ $s[1] != $_SWEETTALK_PROMPT_MTIME ]]; then
    source $SWEETTALKER_CURRENT
    _SWEETTALK_PROMPT_MTIME=$s[1]
  fi
}
(( ${precmd_functions[(I)_sweettalk_prompt_reload]} )) || \
  precmd_functions+=(_sweettalk_prompt_reload)

# Apply the session's current look on shell start, then load its prompt.
command -v sweettalk >/dev/null && sweettalk startup
[[ -f $SWEETTALKER_CURRENT ]] && source $SWEETTALKER_CURRENT
