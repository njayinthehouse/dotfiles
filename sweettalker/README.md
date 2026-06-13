# sweettalker

Roll and rate whole terminal **looks**, and (Stage 2) learn which ones you like.

A *look* is one value per lever:

| Lever | What it sets | Applied via |
|---|---|---|
| `prompt` | the zsh prompt | `current.zsh` (PROMPT) |
| `font` | Alacritty font family | IPC + `sweettalker.toml` |
| `size` | font size | IPC + `sweettalker.toml` |
| `foreground` | text colour | IPC + `sweettalker.toml` |
| `background` | window colour | IPC + `sweettalker.toml` |
| `palette` | the 16 ANSI colours | IPC + `sweettalker.toml` |

You roll a whole look (or tweak one lever) and rate the whole thing **0–10**.
Everything applies **live** to the running Alacritty over its IPC socket, and is
persisted to an imported config file so a freshly-launched Alacritty matches.

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
  Alacritty levers are written to `sweettalker.toml` (imported by
  `alacritty.toml`) **and** pushed live via `alacritty msg config`.
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
