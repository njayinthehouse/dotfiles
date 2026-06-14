# dotfiles

Personal dotfiles for a fresh **Arch Linux** box: `startx` → **nvwm** (a
neovim-driven tiling WM) hosting **neovim**, with **zsh** as the shell. Tracked
as a bare git repo whose work-tree is `$HOME`.

## The `dot` alias

```sh
alias dot='git --git-dir=$HOME/.dotfiles --work-tree=$HOME'
```

Everything is managed through `dot` (e.g. `dot status`, `dot add`, `dot commit`,
`dot push`). Defined in [`.zshrc`](.zshrc).

## Bootstrap on a new machine

```sh
git clone --bare git@github.com:njayinthehouse/dotfiles.git "$HOME/.dotfiles"
alias dot='git --git-dir=$HOME/.dotfiles --work-tree=$HOME'
dot checkout                      # may need to move/rm conflicting defaults
dot config status.showUntrackedFiles no
# once st is a submodule:  dot submodule update --init --recursive
```

Then open a new shell — the self-install blocks in `.zshrc` build/copy the
components into place (see below).

## Components

| Path | What it is |
|------|------------|
| [`nvwm/`](nvwm/README.md) | neovim-driven X11 tiling WM — layout *is* neovim's window layout. Own repo dir; `install.sh` copies into `~/.local`. |
| [`sweettalker/`](sweettalker/README.md) | terminal "look" bandit — rolls/rates fonts & colours, learns preferences (ridge + Thompson sampling). Self-installs to `~/.local/bin/sweettalk`. |
| `st/` *(not tracked yet)* | suckless terminal, built with the from-source clang. Patched `config.h` (Nerd Font + Shift+Return). **To become a submodule** (see TODO). |
| [`.zshrc`](.zshrc) | shell config: aliases, `wifi`/`bt` functions (`--always` auto-connect), PATH for the clang + Lean toolchains, and stamp-guarded self-install blocks for nvwm/sweettalker/st. |
| [`.xinitrc`](.xinitrc) | `startx`: rolls a sweettalker look, launches nvwm, then `st -c nvwm_nvim -e nvim …` as the editor host. |
| [`.config/nvim/`](.config/nvim) | neovim config — minimal `init.lua` + `plugin/nvwm.lua` (fires `nvwm_dirty` autocmds, auto-names panes). |

### Toolchains (installed to `$HOME`, not tracked)
- **clang/LLVM from source** → `~/.local/llvm` (clang, lld, compiler-rt, libc++,
  libunwind). gcc is **not** installed; `~/llvm-project/build-clang.sh` rebuilds
  it (self-hosting). This is the default C/C++ compiler.
- **Lean** via `elan` → `~/.elan` (proof assistant).

### Logs
Runtime logs live in `~/.logs/` (e.g. `nvwm.log`, `clang-build.log`).

## TODO

- [ ] **Finish the st migration.** Smoke-test `st`, then restart X so `st`
  becomes the nvim host (`.xinitrc` already points at it); verify via
  `~/.logs/nvwm.log`. Alacritty stays installed as the fallback until proven.
- [ ] **Make `st` a submodule** of this repo (for reproducibility — st is needed
  on every environment). Fork suckless st → `github.com/njayinthehouse/st` with
  the local `config.h`/`config.mk`/`install.sh` patches committed (keep suckless
  as an `upstream` remote), then `dot submodule add <fork-url> st`.
- [ ] **Phone → on-system Claude bridge.** Be able to text this machine's Claude
  from the phone. Ties into the [`nacjac`](#) home server (SSHFS now, Tailscale
  "phone-from-anywhere" later).
- [ ] **Remove Alacritty** + its config once st is proven as the host.
- [ ] **nvwm:** fix `pane_to_pixels` alignment — the GUI overlay slightly covers
  the status bar (needs a terminal padding offset). Cosmetic.
- [ ] **nvwm:** Firefox tab drag-to-another-window still doesn't move the tab
  (XDND target-finding through the reparented frame).
- [ ] *(optional)* neovim: autocmd to `normal! G` on entering terminal-normal
  mode, so `k` walks up through terminal history instead of landing at the top.

## Done

- From-source clang/LLVM toolchain; gcc dropped.
- Lean installed via elan.
- `wifi`/`bt` shell functions (status / list / connect / disconnect / on/off,
  `--always` for persistent auto-connect).
