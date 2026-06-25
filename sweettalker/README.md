# sweettalker

Generate, rate, and learn whole terminal **looks** — an RL look-generator that
builds looks from scratch, learns your taste from pairwise comparisons, and can
**guess how much you'll like any look**.

A *look* spans five levers:

| Lever | What it sets | Applied via |
|---|---|---|
| `prompt` | the zsh prompt (composed from segments) | `current.zsh` (PROMPT) |
| `font` | st font family | OSC 50 |
| `foreground` | text colour | st default fg (OSC 10) |
| `background` | window colour | st default bg (OSC 11) |
| `palette` | the 16 ANSI colours | st palette (OSC 4) |

Font **size** is not a lever — it's yours to control with the terminal's zoom
(`Ctrl+=` / `Ctrl+-` in st). sweettalk sets only the font *family* (OSC 50
without a size), so st keeps whatever size you've zoomed to across rolls.

Unlike v1 (which picked each lever from a curated list), v2 **generates** looks
from a *genome* — a small bundle of perceptual knobs that `decode` turns into the
concrete colours, font, and prompt. Colours are sampled in **OKLCH** (a
perceptual space) so random genomes still look designed, and the foreground's
lightness is *solved* to hit a target WCAG contrast rather than rejection-sampled.

### One st, every pane

The session lives **inside neovim** (nvwm launches `st -e nvim`; panes are nvim
`:terminal` buffers) under `notermguicolors`, so neovim renders every cell using
st's own ANSI palette and default fg/bg. Recolouring **st** recolours every pane
at once, with no per-nvim push. sweettalk finds the pts st shares with the
session neovim (via `$NVIM`) and writes the OSC escapes there.

The **prompt** is per-shell zsh state, so it can't be pushed into running shells
the way colours can. Instead the engine writes the new prompt to `current.zsh`
and a `precmd` hook re-sources it whenever its mtime changes — so any roll (in
any pane) updates the prompt everywhere on the next prompt draw.

## Install

```sh
sh install.sh        # binary + .zshrc source line (self-installs on repo change)
```

## How it learns

- **Rate by comparison.** `look duel` shows two looks (applied live) and you pick
  the one you like more. Each comparison trains a **Bradley–Terry reward model**
  — a Bayesian logistic regression (Newton/IRLS + Laplace covariance, pure
  Python, no numpy) over ~40 continuous features of a look. `<lever> duel`
  varies only that lever, so the comparison trains just that lever's features.
  - **`duel`** picks the most *informative* opponent (active learning): from a
    candidate pool, the look whose winner the model can least predict — utility
    closest to your current look (a coin-flip outcome) and high posterior
    uncertainty. That's the comparison that teaches the model the most.
  - **`refine`** instead pits the current look against a small local *tweak* of
    itself (gaussian nudges to colours, occasional prompt flips), for
    polishing a theme you already like. `<lever> refine` nudges just that lever.
  - Winner sticks: the look you prefer becomes current (tie/skip leaves it).
- **Guess a rating.** `guess` predicts the current look's **0–10** rating as its
  percentile of utility among random looks, with the model's uncertainty.
- **Sliding explore/exploit.** Each roll samples K candidate genomes and picks
  one by softmax over standardized predicted utility:
  - **EXPLOIT** favours high predicted utility — high score → high probability.
  - **EXPLORE** flips the sign (low predicted favoured) *and* adds an
    uncertainty bonus (looks the model is least sure about).
  The **exploration rate** (probability a roll is EXPLORE) auto-decays as
  comparisons accumulate, can be pinned (`look explore 0.3`), or forced per roll
  (`look roll explore` / `look roll exploit`).
- **Warm start.** Your old v1 0–10 ratings are migrated once into synthetic
  comparisons (every pair with a clear score gap), so the model isn't cold.

## Commands

| Command | Action |
|---|---|
| `look` | show the current look + its predicted rating |
| `look roll [explore\|exploit]` | generate & apply a new look (optional forced mode) |
| `look duel` | rate vs the most informative opponent (active learning) |
| `look refine` | rate vs a small local tweak (polish the current look) |
| `look explore [0..1\|auto]` | show/set/auto the exploration rate |
| `look auto [on\|off]` | roll a fresh look on every session start (default on) |
| `<lever>` | show that lever's current value |
| `<lever> roll [explore\|exploit]` | re-roll just that lever |
| `<lever> duel` | duel two looks differing only in that lever |
| `<lever> refine` | duel the current look vs a small tweak of that lever |
| `guess` | predict the current look's 0–10 rating |
| `learned` | show what the model has learned you like/dislike |

`look`, `prompt`, `font`, `foreground`, `background`, `palette`, `guess`,
and `learned` are aliases in `~/.zshrc`. (Colour levers are `foreground` /
`background`, not `fg`/`bg`, which are zsh job-control builtins.)

### Live (program-running) prompt segments

`prompt_subst` is on and the PROMPT is written single-quoted, so `$(...)` /
`%(...)` reach the shell verbatim. Composable segments include `time` (`%T`),
`load` (`$(cut -d" " -f1 /proc/loadavg)`), and a status-aware glyph that turns
green/red on the last command's exit status.

## How it works

- **State:** `~/.local/share/sweettalker/state.json` — the current genome, the
  list of pairwise `comparisons`, autoroll flag, and exploration-rate override.
- **Genome → decode:** colours in OKLCH (background lightness/hue, a foreground
  contrast target, a palette hue-shift/chroma anchored to the background so the
  16 ANSI colours are coherent yet keep red≈red); a font family (size is left to
  the terminal's zoom); a compositional prompt (ordered segments + glyph + layout,
  coloured by ANSI index so the prompt tracks the palette).
- **Apply:** the prompt goes to `current.zsh` (re-sourced live by the precmd
  hook); colours/font are painted onto the live st via OSC escapes.

## Tests

```sh
python3 test_st.py     # color roundtrip, decode legibility, model, policy, levers
```

## Extending

- **More fonts:** `pacman -S` them — the `font` lever auto-discovers monospace
  families.
- **More prompt segments / glyphs:** extend `SEG_POOL` / `GLYPHS` in
  `sweettalker.py`.
- **Feature the model can learn on:** add to `feature_vector` / `FEATURE_NAMES`.
