# sweettalker

Roll and rate whole terminal **looks**, and (Stage 2) learn which ones you like.

A *look* is one value per lever:

| Lever | What it sets | Applied via |
|---|---|---|
| `prompt` | the zsh prompt | `current.zsh` (PROMPT) |
| `font` | st font family | OSC 50 |
| `size` | font size | OSC 50 |
| `foreground` | text colour | st default fg (OSC 10) |
| `background` | window colour | st default bg (OSC 11) |
| `palette` | the 16 ANSI colours | st palette (OSC 4) |

You roll a whole look (or tweak one lever) and rate the whole thing **0–10**. The
colour and font levers apply **live** to the running st via OSC escape sequences.

### One st, every pane

The session lives **inside neovim** (nvwm launches `st -e nvim`, and panes are
neovim `:terminal` buffers) under `notermguicolors`, so neovim renders every cell
using st's own ANSI palette and default fg/bg — it owns no colours of its own.
Recolouring **st** therefore recolours every pane at once, with no per-nvim push.

sweettalk runs inside a neovim `:terminal`, so its own stdout reaches neovim, not
the outer st. It finds the pts that st shares with the session neovim (via the
`$NVIM` process's stdout fd) and writes the OSC escapes there, so st reads and
applies them itself:

- `font`/`size` → `OSC 50 ; <family>:size=<n>` (st reloads the font and reflows)
- `foreground` → `OSC 10` (st default foreground)
- `background` → `OSC 11` (st default background)
- `palette` → `OSC 4 ; N ; <hex>` for N in 0..15 (normal[0..7] then bright[0..7])

st marks every cell dirty on a colour change and redraws/resizes on a font
change, so the running neovim recolours and reflows immediately — no
`:colorscheme` interaction, because sweettalk never touches neovim's highlights.

## Install

```sh
sh install.sh        # binary + .zshrc source line (self-installs on repo change)
```

## Commands

`look`, `prompt`, `font`, `size`, `foreground`, `background`, `palette`, and
`confide` are aliases in `~/.zshrc`. (The colour levers are `foreground` /
`background`, not `fg`/`bg`, because those are zsh job-control builtins.)

| Command | Action |
|---|---|
| `look` | show the current look |
| `look roll` | roll a whole new look (all levers) and apply it |
| `look auto [on\|off]` | roll a fresh look on every shell start (default on) |
| `<lever>` | show that lever's current value (e.g. `font`, `size`) |
| `<lever> roll` | re-roll just that lever, keep the rest |
| `<lever> help` | usage |
| `confide` | rate the current look 0–10 (interactive, skippable) |
| `look rate <0-10>` | rate non-interactively |
| `learned` | show what the model has learned you like/dislike |

### Live (program-running) prompts

A few prompts run code on **every** render (`prompt_subst` is on and the PROMPT
is written single-quoted, so `$(...)` / `%(...)` reach the shell verbatim):

- `clock` — a live `$(date +%H:%M:%S)` that ticks each prompt draw
- `status-face` — a `%(?.…)` happy/sad face for the last command's exit status
- `loadavg` — 1-minute load from `$(cut -d" " -f1 /proc/loadavg)`

## How it works

- State: `~/.local/share/sweettalker/state.json` — the current look, autoroll
  flag, and the list of `{look, rating}` you've confided.
- The prompt is written to `current.zsh` (sourced by `sweettalker.zsh`); the
  colour and font levers are painted onto the live st via OSC escapes written to
  the pts st shares with the session neovim (best-effort; only when `$NVIM` is
  set and `SWEETTALKER_NO_IPC` is not).
- Colours stay legible: candidate looks are contrast-filtered so the foreground
  is never too close to the background.

## Roadmap

- **Stage 1 (done):** the six levers, live apply + persist, whole/per-lever
  random rolls, `confide` 0–10 collecting ratings.
- **Stage 2 (done):** a Bayesian linear bandit (ridge + Thompson sampling, pure
  Python — no numpy) over ~40 *features* of a look (prompt shape/symbol/git/time,
  font ligatures/bitmap/width/style, size, fg/bg luminance + hue bucket, palette).
  Each 0–10 rating updates a posterior over per-feature weights, so it learns
  *what* you like (e.g. ligatures, two-line prompts, dark backgrounds),
  generalises to looks it hasn't shown you, and rolls toward promising ones via
  Thompson sampling. Below `LEARN_MIN` (8) ratings, rolls stay exactly random.
  The `learned` command prints the top +/- weights in plain English.

## Extending

- **More prompts:** append to `PROMPTS` in `sweettalker.py`.
- **More fonts:** `pacman -S` them — the `font` lever auto-discovers monospace
  families.
- **More colours / palettes:** extend `FOREGROUNDS`, `BACKGROUNDS`, `PALETTES`.
