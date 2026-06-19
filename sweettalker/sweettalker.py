#!/usr/bin/env python3
"""sweettalker — roll and rate whole terminal "looks".

A *look* is one value per lever:
  prompt      the zsh prompt (curated pool)
  font        the st font family (auto-discovered monospace)
  size        the font size
  foreground  the text colour       (st default foreground, OSC 10)
  background  the window colour      (st default background, OSC 11)
  palette     the 16 ANSI colours    (st palette, OSC 4)

The session lives inside neovim (nvwm runs `st -e nvim`, panes are nvim :terminal
buffers) under `notermguicolors`, so neovim renders every cell in st's own ANSI
palette and default fg/bg. Recolouring st therefore recolours every pane — no
per-nvim highlight push needed. A look is painted onto the live st as OSC escapes
(font OSC 50, fg/bg OSC 10/11, palette OSC 4) written to the pts st shares with
the session neovim ($NVIM).

You roll a whole look (or tweak one lever) and rate the whole thing 0-10. In
Stage 1 rolls are random (contrast-filtered so looks stay readable) and ratings
are just collected; the learning layer that turns those ratings into smart rolls
comes in Stage 2.

Usage:
  sweettalk look [roll|auto [on|off]|rate <0-10>|help]     # the whole look
  sweettalk <lever> [roll|help]        lever = prompt|font|size|foreground|
                                              background|palette
  sweettalk confide                    rate the look 0-10 + thumb each part (interactive)
  sweettalk status                     show the current look
  sweettalk learned                    show what the model has learned you like
  sweettalk session                    startx-session hook (roll if autoroll, apply)
  sweettalk startup                    shell-start hook (apply current look)

Stage 2 (the learning layer) is live: once you have LEARN_MIN ratings, rolls use
a Bayesian linear bandit (ridge + Thompson sampling, pure Python) over features
of a look, so they generalise toward looks you'll like. Below the threshold rolls
stay random.
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("SWEETTALKER_DATA",
                               Path.home() / ".local/share/sweettalker"))
STATE = DATA_DIR / "state.json"
PROMPT_CURRENT = DATA_DIR / "current.zsh"

SCALE = 10
LEVERS = ["prompt", "font", "size", "foreground", "background", "palette"]
MIN_CONTRAST = 4.0          # WCAG-ish; keeps fg/bg legible


# ---------------------------------------------------------------- prompt lever
G = "${vcs_info_msg_0_}"
PROMPTS = [
    ("minimal",       "%F{green}❯%f "),
    ("arrow",         "%F{cyan}%~%f" + G + " %F{green}❯%f "),
    ("arrow-status",  "%F{blue}%~%f" + G + " %(?.%F{green}.%F{red})❯%f "),
    ("classic",       "%n@%m %~" + G + " %# "),
    ("classic-color", "%F{green}%n@%m%f:%F{blue}%~%f" + G + " %# "),
    ("lambda",        "%F{magenta}λ%f %F{blue}%~%f" + G + " "),
    ("bracket",       "[%F{yellow}%~%f" + G + "] $ "),
    ("dollar",        "%F{green}%~%f" + G + " $ "),
    ("angle",         "%F{blue}%~%f" + G + " %F{magenta}»%f "),
    ("chevron",       "%F{cyan}%~%f" + G + " %F{yellow}›%f "),
    ("star",          "%F{yellow}%~%f" + G + " ★ "),
    ("dim",           "%F{8}%~%f" + G + " %F{8}❯%f "),
    ("time",          "%F{8}%T%f %F{blue}%~%f" + G + " ❯ "),
    ("host",          "%F{green}%m%f %F{blue}%~%f" + G + " › "),
    ("two-line",      "%F{blue}%~%f" + G + "\n%F{green}❯%f "),
    ("two-line-box",  "%F{cyan}╭─%f %F{blue}%~%f" + G + "\n%F{cyan}╰─%f❯ "),
    # program-running prompts: code runs every render (prompt_subst is on, and the
    # PROMPT is written single-quoted so $(...) / %(...) reach the shell verbatim).
    ("clock",         "%F{8}$(date +%H:%M:%S)%f %F{blue}%~%f" + G + " ❯ "),
    ("status-face",   "%F{blue}%~%f" + G + " %(?.%F{green}^_^.%F{red}x_x)%f "),
    ("loadavg",       "%F{yellow}$(cut -d\" \" -f1 /proc/loadavg)%f "
                      "%F{blue}%~%f" + G + " ❯ "),
]
PROMPT_MAP = dict(PROMPTS)
PROMPT_NAMES = [n for n, _ in PROMPTS]


# ------------------------------------------------------------------ font lever
WEIGHTS = {
    "thin", "th", "extralight", "extlt", "ultralight", "ultlt", "ulight",
    "semilight", "semlt", "light", "lt", "regular", "reg", "rg", "book",
    "text", "normal", "medium", "med", "md", "semibold", "semibd", "sembd",
    "smbd", "sb", "demibold", "demi", "db", "bold", "bd", "extrabold",
    "extbd", "eb", "ultrabold", "ultbd", "ub", "black", "blk", "bk", "heavy",
    "hv", "ultrablack", "retina", "ret", "oblique", "obl", "italic", "ital",
    "it", "condensed", "cond", "semicondensed", "semicond", "semcond", "smcond",
    "extracondensed", "extcond", "expanded", "extd", "extended", "exp", "narrow",
}
MARKERS = {"nerd", "font", "nf", "nfm", "nfp", "mono", "propo", "nl", "pl"}
SYMBOL_ONLY = ("symbols nerd font",)


def _has_weight(low):
    return any(t in WEIGHTS for t in low.split())


def _norm_key(fam):
    toks = [t for t in fam.lower().split() if t not in WEIGHTS and t not in MARKERS]
    return " ".join(toks)


def _pick_rep(cands):
    low = {c: c.lower() for c in cands}
    for pred in (
        lambda cl: "nerd font mono" in cl and not _has_weight(cl),
        lambda cl: "mono" in cl and not _has_weight(cl),
        lambda cl: not _has_weight(cl),
        lambda cl: "nerd font mono" in cl,
        lambda cl: True,
    ):
        m = [c for c, cl in low.items() if pred(cl)]
        if m:
            return min(m, key=len)


def font_values():
    try:
        out = subprocess.run(["fc-list", ":spacing=mono", "family"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ["monospace"]
    families = set()
    for line in out.splitlines():
        for fam in line.split(","):
            fam = fam.strip()
            if fam and not any(s in fam.lower() for s in SYMBOL_ONLY):
                families.add(fam)
    groups = {}
    for fam in families:
        groups.setdefault(_norm_key(fam), []).append(fam)
    arms = sorted({_pick_rep(c) for c in groups.values() if _pick_rep(c)})
    return arms or ["monospace"]


# ------------------------------------------------------------ size/colour levers
SIZES = [10, 11, 12, 13, 14, 15, 16]

FOREGROUNDS = [
    # neutral lights
    "#ebdbb2", "#d8dee9", "#abb2bf", "#f8f8f2", "#cdd6f4", "#a9b1d6",
    "#e0def4", "#ffffff", "#fdf6e3", "#c0caf5",
    # warm (reds/oranges/yellows/pinks)
    "#ffb86c", "#fabd2f", "#f7768e", "#e0af68", "#ff79c6", "#d3869b",
    "#ffcb6b", "#ee6f57",
    # cool (greens/cyans/blues/purples)
    "#8be9fd", "#7dcfff", "#98c379", "#b8bb26", "#50fa7b", "#83a598",
    "#82aaff", "#bb9af7", "#7aa2f7", "#89ddff",
    # darks (legible on light backgrounds)
    "#1d2021", "#282828", "#073642", "#3c3836", "#21222c", "#15161e",
]
BACKGROUNDS = [
    "#1d2021", "#282828", "#2e3440", "#1e1e2e", "#282a36", "#1a1b26",
    "#0d1117", "#191724", "#000000", "#073642",      # dark
    "#fbf1c7", "#eee8d5", "#f5f5f5", "#fdf6e3",      # light
]
PALETTES = {
    "gruvbox": {
        "normal": ["#282828", "#cc241d", "#98971a", "#d79921", "#458588",
                   "#b16286", "#689d6a", "#a89984"],
        "bright": ["#928374", "#fb4934", "#b8bb26", "#fabd2f", "#83a598",
                   "#d3869b", "#8ec07c", "#ebdbb2"]},
    "nord": {
        "normal": ["#3b4252", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1",
                   "#b48ead", "#88c0d0", "#e5e9f0"],
        "bright": ["#4c566a", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1",
                   "#b48ead", "#8fbcbb", "#eceff4"]},
    "dracula": {
        "normal": ["#21222c", "#ff5555", "#50fa7b", "#f1fa8c", "#bd93f9",
                   "#ff79c6", "#8be9fd", "#f8f8f2"],
        "bright": ["#6272a4", "#ff6e6e", "#69ff94", "#ffffa5", "#d6acff",
                   "#ff92df", "#a4ffff", "#ffffff"]},
    "tokyo-night": {
        "normal": ["#15161e", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7",
                   "#bb9af7", "#7dcfff", "#a9b1d6"],
        "bright": ["#414868", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7",
                   "#bb9af7", "#7dcfff", "#c0caf5"]},
    "solarized": {
        "normal": ["#073642", "#dc322f", "#859900", "#b58900", "#268bd2",
                   "#d33682", "#2aa198", "#eee8d5"],
        "bright": ["#002b36", "#cb4b16", "#586e75", "#657b83", "#839496",
                   "#6c71c4", "#93a1a1", "#fdf6e3"]},
}


def _lum(hexs):
    h = hexs.lstrip("#")
    chans = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
           for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def lever_values(name):
    return {"prompt": PROMPT_NAMES, "font": font_values(), "size": SIZES,
            "foreground": FOREGROUNDS, "background": BACKGROUNDS,
            "palette": list(PALETTES)}[name]


# ------------------------------------------------------------------- applying
def _squote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _st_tty():
    """Path of the pts the outer st shares with the session neovim.

    sweettalk runs inside an nvim :terminal, so its own stdout reaches nvim, not
    st. But $NVIM names the session neovim, whose stdout fd is st's side of the
    pts; OSC escapes written there are read and applied by st itself."""
    server = os.environ.get("NVIM")
    if not server:
        return None
    want = server.encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if (proc / "comm").read_text().strip() != "nvim":
                continue
            if want not in (proc / "cmdline").read_bytes():
                continue
            tty = os.readlink(proc / "fd/1")
        except OSError:
            continue
        if tty.startswith("/dev/pts/"):
            return tty
    return None


def st_osc(look):
    """The OSC escape string that paints a look onto st: font + size (OSC 50),
    foreground (OSC 10), background (OSC 11), and the 16 ANSI colours (OSC 4;n).
    Each is BEL-terminated. Pure: no I/O."""
    pal = PALETTES[look["palette"]]
    seqs = [f'\033]50;{look["font"]}:size={float(look["size"])}\007',
            f'\033]10;{look["foreground"]}\007',
            f'\033]11;{look["background"]}\007']
    colors16 = pal["normal"] + pal["bright"]      # ANSI 0..15
    seqs += [f'\033]4;{i};{c}\007' for i, c in enumerate(colors16)]
    return "".join(seqs)


def st_apply(look):
    """Paint the colour + font levers onto the live st (best-effort). No-op
    unless $NVIM is set (it names st's pts) and IPC isn't suppressed. st marks
    every cell dirty on a colour change and reloads/redraws on a font change, so
    the running neovim recolours and reflows without any per-nvim push."""
    if os.environ.get("SWEETTALKER_NO_IPC"):
        return
    tty = _st_tty()
    if not tty:
        return
    try:
        with open(tty, "wb") as f:
            f.write(st_osc(look).encode())
    except OSError:
        pass


def apply_look(look):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_CURRENT.write_text("PROMPT=" + _squote(PROMPT_MAP[look["prompt"]]) + "\n")
    st_apply(look)


# --------------------------------------------------------------------- state
def load_all():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_all(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _colors_with_contrast(options, other):
    return [c for c in options if contrast(c, other) >= MIN_CONTRAST]


def random_look():
    for _ in range(200):
        fg, bg = random.choice(FOREGROUNDS), random.choice(BACKGROUNDS)
        if contrast(fg, bg) >= MIN_CONTRAST:
            break
    else:
        fg, bg = "#ebdbb2", "#1d2021"
    return {"prompt": random.choice(PROMPT_NAMES),
            "font": random.choice(font_values()),
            "size": random.choice(SIZES),
            "foreground": fg, "background": bg,
            "palette": random.choice(list(PALETTES))}


def ensure_current(state):
    cur = state.setdefault("current", {})
    base = random_look()
    for k in LEVERS:
        cur.setdefault(k, base[k])
    if cur["font"] not in font_values():
        cur["font"] = random.choice(font_values())
    if cur["prompt"] not in PROMPT_MAP:
        cur["prompt"] = random.choice(PROMPT_NAMES)
    if cur["palette"] not in PALETTES:
        cur["palette"] = random.choice(list(PALETTES))
    return cur


def roll_lever(state, name):
    cur = ensure_current(state)
    if name == "foreground":
        opts = _colors_with_contrast(FOREGROUNDS, cur["background"]) or FOREGROUNDS
    elif name == "background":
        opts = _colors_with_contrast(BACKGROUNDS, cur["foreground"]) or BACKGROUNDS
    else:
        opts = lever_values(name)
    opts = [v for v in opts if v != cur[name]] or opts
    cur[name] = random.choice(opts)
    return cur[name]


# =============================================================== Stage 2: learn
# A shallow Bayesian linear bandit over FEATURES of a look. Ratings (0-10) train
# a ridge-regression posterior; Thompson sampling rolls toward looks the user
# will like. Pure stdlib — no numpy.

LEARN_MIN = 8               # ratings before learning kicks in; below = random
RIDGE_LAMBDA = 1.0          # ridge prior strength (A = λI + Σ xxᵀ)
TS_CANDIDATES = 64          # candidate looks scored per Thompson roll


# ----- prompt feature table: (one_line, sym_class, shows_git, shows_time) -----
# sym_class buckets the trailing prompt symbol; values are stable feature names.
PROMPT_SYMS = {
    "minimal": "chevron", "arrow": "chevron", "arrow-status": "chevron",
    "classic": "hash", "classic-color": "hash", "lambda": "lambda",
    "bracket": "dollar", "dollar": "dollar", "angle": "guillemet",
    "chevron": "angle-quote", "star": "star", "dim": "chevron",
    "time": "chevron", "host": "guillemet", "two-line": "chevron",
    "two-line-box": "chevron", "clock": "chevron", "status-face": "face",
    "loadavg": "chevron",
}
PROMPT_SYM_CLASSES = ["chevron", "hash", "lambda", "dollar", "guillemet",
                      "angle-quote", "star", "face"]


def _prompt_feats(name):
    body = PROMPT_MAP.get(name, "")
    two_line = "\n" in body
    shows_git = G in body
    shows_time = ("%T" in body) or ("date " in body) or ("%D" in body)
    sym = PROMPT_SYMS.get(name, "chevron")
    feats = [0.0 if two_line else 1.0,      # one_line
             1.0 if shows_git else 0.0,
             1.0 if shows_time else 0.0]
    feats += [1.0 if sym == c else 0.0 for c in PROMPT_SYM_CLASSES]
    names = ["prompt.one_line", "prompt.shows_git", "prompt.shows_time"]
    names += [f"prompt.sym.{c}" for c in PROMPT_SYM_CLASSES]
    return feats, names


# ----- font heuristics: family name + a small curated table, with fallbacks ---
# Ligature-capable families (curated; substring match, lowercased).
LIGATURE_FONTS = (
    "firacode", "fira code", "cascadia", "caskaydia", "jetbrains", "victor",
    "hasklug", "hasklig", "monoid", "lilex", "d2coding", "iosevka", "commit",
    "geistmono", "geist mono", "monaspice", "recmono", "comicshanns", "intone",
    "zedmono", "zed mono", "fantasque", "blexmono", "code new roman",
)
# Bitmap / pixel fonts (curated).
BITMAP_FONTS = (
    "gohufont", "proggy", "terminus", "terminess", "profont", "bigblueterm",
    "bitstrom", "3270", "shuretech", "envycoder",
)
WIDTH_WIDE = ("wide", "expanded", "extended", "extd", "exp")
WIDTH_NARROW = ("cond", "narrow", "compress")
STYLE_SLAB = ("slab",)
STYLE_HAND = ("comic", "fantasque", "opendyslexic", "monofur", "shanns",
              "casual", "handwrit")


def _font_feats(family):
    low = family.lower()
    lig = any(s in low for s in LIGATURE_FONTS)
    bitmap = any(s in low for s in BITMAP_FONTS)
    wide = any(s in low for s in WIDTH_WIDE)
    narrow = any(s in low for s in WIDTH_NARROW)
    slab = any(s in low for s in STYLE_SLAB)
    hand = any(s in low for s in STYLE_HAND)
    feats = [1.0 if lig else 0.0,
             1.0 if bitmap else 0.0,
             1.0 if wide else 0.0,
             1.0 if narrow else 0.0,
             1.0 if slab else 0.0,
             1.0 if hand else 0.0]
    names = ["font.ligatures", "font.bitmap", "font.wide", "font.narrow",
             "font.slab", "font.handwritten"]
    return feats, names


# ----- colour features: luminance + a coarse hue bucket --------------------
HUE_BUCKETS = ["red", "yellow", "green", "cyan", "blue", "magenta", "gray"]


def _hue_bucket(hexs):
    h = hexs.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 0.10:                       # near-neutral
        return "gray"
    if mx == r:
        deg = ((g - b) / (mx - mn)) % 6
    elif mx == g:
        deg = (b - r) / (mx - mn) + 2
    else:
        deg = (r - g) / (mx - mn) + 4
    deg *= 60
    if deg < 30 or deg >= 330:
        return "red"
    if deg < 90:
        return "yellow"
    if deg < 150:
        return "green"
    if deg < 210:
        return "cyan"
    if deg < 270:
        return "blue"
    return "magenta"


def _color_feats(hexs, prefix):
    feats = [_lum(hexs)]
    feats += [1.0 if _hue_bucket(hexs) == b else 0.0 for b in HUE_BUCKETS]
    names = [f"{prefix}.luminance"]
    names += [f"{prefix}.hue.{b}" for b in HUE_BUCKETS]
    return feats, names


# ----- the full feature vector ------------------------------------------------
SIZE_MIN, SIZE_MAX = float(min(SIZES)), float(max(SIZES))
PALETTE_KEYS = list(PALETTES)


def _size_feats(size):
    sz = float(size)
    span = (SIZE_MAX - SIZE_MIN) or 1.0
    norm = (sz - SIZE_MIN) / span
    feats = [norm,
             1.0 if sz <= SIZE_MIN + 1 else 0.0,    # tiny
             1.0 if sz >= SIZE_MAX - 1 else 0.0]     # large
    names = ["size.norm", "size.tiny", "size.large"]
    return feats, names


def _palette_feats(name):
    feats = [1.0 if name == k else 0.0 for k in PALETTE_KEYS]
    names = [f"palette.{k}" for k in PALETTE_KEYS]
    return feats, names


def _build_features(look):
    """Return (values, names) for a look. Both lists are parallel and stable."""
    vals, names = [1.0], ["bias"]
    for getter in (
        lambda: _prompt_feats(look["prompt"]),
        lambda: _font_feats(look["font"]),
        lambda: _size_feats(look["size"]),
        lambda: _color_feats(look["foreground"], "fg"),
        lambda: _color_feats(look["background"], "bg"),
        lambda: _palette_feats(look["palette"]),
    ):
        v, n = getter()
        vals += v
        names += n
    return vals, names


# FEATURE_NAMES: stable order, computed once from a reference look.
FEATURE_NAMES = _build_features({
    "prompt": PROMPT_NAMES[0], "font": "monospace", "size": SIZES[0],
    "foreground": FOREGROUNDS[0], "background": BACKGROUNDS[0],
    "palette": PALETTE_KEYS[0]})[1]
N_FEATURES = len(FEATURE_NAMES)


def features(look):
    """Feature vector for a look, aligned to FEATURE_NAMES."""
    return _build_features(look)[0]


# ----------------------------------------------------- pure-Python linear algebra
def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _matvec(M, v):
    return [_dot(row, v) for row in M]


def _outer_add(M, v, scale):
    """In-place M += scale * v vᵀ."""
    for i, vi in enumerate(v):
        s = scale * vi
        if s == 0.0:
            continue
        row = M[i]
        for j, vj in enumerate(v):
            row[j] += s * vj


def _cholesky(A):
    """Lower-triangular L with L Lᵀ = A (A symmetric positive-definite)."""
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                d = A[i][i] - s
                L[i][j] = (d ** 0.5) if d > 1e-12 else 1e-6
            else:
                L[i][j] = (A[i][j] - s) / L[j][j]
    return L


def _solve_chol(L, b):
    """Solve (L Lᵀ) x = b given lower-triangular L."""
    n = len(L)
    y = [0.0] * n
    for i in range(n):                       # forward: L y = b
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):           # back: Lᵀ x = y
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


def _sample_posterior(mean, L, sigma):
    """Draw w ~ N(mean, σ²A⁻¹), where A = L Lᵀ.

    If z ~ N(0,I) then x solving Lᵀ x = z has cov (L Lᵀ)⁻¹ = A⁻¹, so
    mean + σ x ~ N(mean, σ²A⁻¹).
    """
    n = len(mean)
    z = [random.gauss(0.0, 1.0) for _ in range(n)]
    x = [0.0] * n                            # solve Lᵀ x = z (back-substitution)
    for i in range(n - 1, -1, -1):
        x[i] = (z[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return [m + sigma * xi for m, xi in zip(mean, x)]


def _linalg_selfcheck():
    """Tiny sanity check: solve a known SPD system; return True on success."""
    A = [[4.0, 1.0], [1.0, 3.0]]
    b = [1.0, 2.0]
    L = _cholesky(A)
    x = _solve_chol(L, b)
    # verify A x ≈ b
    r = _matvec(A, x)
    return all(abs(a - c) < 1e-9 for a, c in zip(r, b))


# ------------------------------------------------------------- the bandit model
# Maps a lever to the prefix its features carry in FEATURE_NAMES, so a
# per-component thumb can be turned into an observation that touches only that
# lever's weights (see _masked_features / fit_model).
LEVER_PREFIX = {
    "prompt": "prompt.", "font": "font.", "size": "size.",
    "foreground": "fg.", "background": "bg.", "palette": "palette.",
}


def _masked_features(x, lever):
    """A feature vector that is `x` only inside `lever`'s own feature group and
    zero everywhere else (bias included). Used so a component thumbs-up/down
    trains just that lever's weights, leaving the rest of the look uncredited."""
    prefix = LEVER_PREFIX[lever]
    return [v if FEATURE_NAMES[i].startswith(prefix) else 0.0
            for i, v in enumerate(x)]


def _add_obs(A, b, x, r):
    """Fold one (feature-vector, target) observation into the ridge normal eqns:
    A += x xᵀ ;  b += r x."""
    _outer_add(A, x, 1.0)
    for i, xi in enumerate(x):
        b[i] += r * xi


def fit_model(ratings):
    """Ridge posterior from rated looks.

    A = λI + Σ xxᵀ ;  b = Σ r x ;  mean w = A⁻¹b ;  cov = σ²A⁻¹.
    Each entry contributes a whole-look observation (when it carries a 0-SCALE
    rating) and/or one masked single-lever observation per component thumb
    (yes → SCALE, no → 0) that trains only that lever's features. Only the real
    whole-look ratings drive sigma. Returns dict with mean, the Cholesky L of A,
    and sigma. Cold-start safe.
    """
    n = N_FEATURES
    A = [[0.0] * n for _ in range(n)]
    for i in range(n):
        A[i][i] = RIDGE_LAMBDA
    bvec = [0.0] * n
    rs = []
    for entry in ratings:
        look = entry.get("look")
        if not look:
            continue
        try:
            x = features(look)
        except (KeyError, ValueError):
            continue
        rating = entry.get("rating")
        if rating is not None:
            r = float(rating)
            rs.append(r)
            _add_obs(A, bvec, x, r)
        for lever, keep in (entry.get("components") or {}).items():
            if lever in LEVER_PREFIX:
                _add_obs(A, bvec, _masked_features(x, lever),
                         float(SCALE) if keep else 0.0)
    L = _cholesky(A)
    mean = _solve_chol(L, bvec)
    # noise scale: spread of real ratings, floored so exploration never collapses.
    if len(rs) >= 2:
        m = sum(rs) / len(rs)
        var = sum((v - m) ** 2 for v in rs) / len(rs)
        sigma = max(var ** 0.5, 1.0)
    else:
        sigma = float(SCALE)
    return {"mean": mean, "L": L, "sigma": sigma, "n": len(rs)}


def _score(w, look):
    return _dot(w, features(look))


def thompson_look(model):
    """Sample a w, generate K contrast-valid candidate looks, pick the best."""
    w = _sample_posterior(model["mean"], model["L"], model["sigma"])
    best, best_s = None, None
    for _ in range(TS_CANDIDATES):
        cand = random_look()                 # already contrast-filtered
        s = _score(w, cand)
        if best_s is None or s > best_s:
            best, best_s = cand, s
    return best or random_look()


def thompson_lever(model, cur, name):
    """Vary one lever (others fixed), keeping fg/bg contrast like roll_lever."""
    w = _sample_posterior(model["mean"], model["L"], model["sigma"])
    if name == "foreground":
        opts = _colors_with_contrast(FOREGROUNDS, cur["background"]) or FOREGROUNDS
    elif name == "background":
        opts = _colors_with_contrast(BACKGROUNDS, cur["foreground"]) or BACKGROUNDS
    else:
        opts = lever_values(name)
    opts = [v for v in opts if v != cur[name]] or opts
    best, best_s = None, None
    for v in opts:
        trial = dict(cur)
        trial[name] = v
        s = _score(w, trial)
        if best_s is None or s > best_s:
            best, best_s = v, s
    return best if best is not None else random.choice(opts)


def _learning_on(state):
    return len(state.get("ratings", [])) >= LEARN_MIN


def learned_look(state):
    """A whole look chosen by the model (caller guarantees learning is on)."""
    return thompson_look(fit_model(state["ratings"]))


def learned_lever(state, name):
    """Choose one lever via the model; updates state['current'] like roll_lever."""
    cur = ensure_current(state)
    cur[name] = thompson_lever(fit_model(state["ratings"]), cur, name)
    return cur[name]


# ------------------------------------------------------------------- commands
def show_look(look):
    print(f"  prompt     {look['prompt']}")
    print(f"  font       {look['font']}  {look['size']}pt")
    print(f"  foreground {look['foreground']}")
    print(f"  background {look['background']}")
    print(f"  palette    {look['palette']}")


def cmd_status():
    state = load_all()
    cur = ensure_current(state)
    n = len(state.get("ratings", []))
    auto = "on" if state.get("autoroll", True) else "off"
    print(f"current look   (autoroll {auto}, {n} ratings)")
    show_look(cur)
    return 0


def _ask_rating(label):
    try:
        raw = input(f"  rate {label} 0-{SCALE} (Enter to skip) > ").strip()
    except EOFError:
        print()
        return None
    if raw == "" or raw.lower() in ("s", "skip"):
        return None
    if raw.isdigit() and 0 <= int(raw) <= SCALE:
        return int(raw)
    print(f"    not 0-{SCALE} — skipped")
    return None


def _ask_yesno(label):
    """Optional thumb for one component: True (y), False (n), or None (skip)."""
    try:
        raw = input(f"    {label}  keep? (y/n, Enter skips) > ").strip().lower()
    except EOFError:
        print()
        return None
    if raw in ("y", "yes"):
        return True
    if raw in ("n", "no"):
        return False
    return None


def _record_rating(state, value):
    cur = state["current"]
    state.setdefault("ratings", []).append({"look": dict(cur), "rating": value})
    save_all(state)
    print(f"    ✓ {value}/{SCALE}  ({len(state['ratings'])} ratings so far)")


def _record_confide(state, rating, components):
    """Append one confide entry carrying an optional whole-look rating and/or
    optional per-component thumbs, then report what landed."""
    entry = {"look": dict(state["current"])}
    if rating is not None:
        entry["rating"] = rating
    if components:
        entry["components"] = components
    state.setdefault("ratings", []).append(entry)
    save_all(state)
    bits = []
    if rating is not None:
        bits.append(f"{rating}/{SCALE}")
    if components:
        bits.append(" ".join(f"{k}{'+' if v else '-'}"
                             for k, v in components.items()))
    print(f"    ✓ recorded {'; '.join(bits)}  ({len(state['ratings'])} entries)")


def cmd_confide(args):
    state = load_all()
    cur = ensure_current(state)
    print("confide — rate the whole look, then thumb each part (Enter skips any)\n")
    show_look(cur)
    print()
    rating = _ask_rating("this whole look")
    components = {}
    for lever in LEVERS:
        verdict = _ask_yesno(f"{lever:<11}({cur[lever]})")
        if verdict is not None:
            components[lever] = verdict
    if rating is None and not components:
        print("    nothing recorded")
        return 0
    _record_confide(state, rating, components)
    return 0


def cmd_look(args):
    if not args:
        return cmd_status()
    sub = args[0]
    if sub == "roll":
        state = load_all()
        ensure_current(state)
        if _learning_on(state):
            state["current"] = learned_look(state)
            note = "sweettalk: new look (learned)\n"
        else:
            state["current"] = random_look()
            note = "sweettalk: new look\n"
        apply_look(state["current"])
        save_all(state)
        sys.stderr.write(note)
        show_look(state["current"])
        return 0
    if sub == "rate":
        if len(args) < 2 or not args[1].isdigit() or not 0 <= int(args[1]) <= SCALE:
            sys.stderr.write(f"usage: look rate <0-{SCALE}>\n")
            return 2
        state = load_all()
        ensure_current(state)
        _record_rating(state, int(args[1]))
        return 0
    if sub == "auto":
        state = load_all()
        if len(args) < 2:
            print("on" if state.get("autoroll", True) else "off")
            return 0
        state["autoroll"] = args[1].lower() in ("on", "true", "1", "yes")
        save_all(state)
        sys.stderr.write(f"sweettalk: autoroll "
                         f"{'on' if state['autoroll'] else 'off'}\n")
        return 0
    if sub == "help":
        sys.stderr.write(__doc__)
        return 0
    sys.stderr.write(__doc__)
    return 2


def cmd_lever(name, args):
    if not args:                                  # bare lever -> show value
        print(ensure_current(load_all())[name])
        return 0
    if args[0] == "roll":
        state = load_all()
        if _learning_on(state):
            learned_lever(state, name)
        else:
            roll_lever(state, name)
        apply_look(state["current"])
        save_all(state)
        sys.stderr.write(f"sweettalk {name}: {state['current'][name]}\n")
        return 0
    if args[0] == "help":
        sys.stderr.write(__doc__)
        return 0
    sys.stderr.write(__doc__)
    return 2


def cmd_session():
    """startx-session hook: roll a fresh look once per session if autoroll is on,
    then apply. Called from ~/.xinitrc. Every shell in the session then just
    applies this look via `startup`, so the whole session shares one look."""
    state = load_all()
    ensure_current(state)
    if state.get("autoroll", True):
        if _learning_on(state):
            state["current"] = learned_look(state)
        else:
            state["current"] = random_look()
    apply_look(state["current"])
    save_all(state)
    return 0


def cmd_startup():
    """shell-start hook: apply the session's current look, never roll. The roll
    is the session's job (`session`, from ~/.xinitrc), so opening a new pane or
    shell doesn't change the look out from under you."""
    state = load_all()
    ensure_current(state)
    apply_look(state["current"])
    save_all(state)
    return 0


def _humanize(name):
    """Turn a FEATURE_NAMES key into plain English for the `learned` readout."""
    pretty = {
        "prompt.one_line": "one-line prompt", "prompt.shows_git": "git in prompt",
        "prompt.shows_time": "time in prompt", "font.ligatures": "ligature fonts",
        "font.bitmap": "bitmap fonts", "font.wide": "wide fonts",
        "font.narrow": "narrow fonts", "font.slab": "slab fonts",
        "font.handwritten": "handwritten fonts", "size.norm": "larger size",
        "size.tiny": "tiny size", "size.large": "large size",
        "fg.luminance": "bright foreground", "bg.luminance": "bright background",
    }
    if name in pretty:
        return pretty[name]
    if name.startswith("prompt.sym."):
        return name.split(".")[-1] + " prompt symbol"
    if name.startswith("fg.hue."):
        return name.split(".")[-1] + " foreground"
    if name.startswith("bg.hue."):
        return name.split(".")[-1] + " background"
    if name.startswith("palette."):
        return name.split(".")[-1] + " palette"
    return name


def cmd_learned():
    state = load_all()
    ratings = state.get("ratings", [])
    n = len(ratings)
    if n < LEARN_MIN:
        print(f"learning off — {n}/{LEARN_MIN} ratings "
              f"(rolls are random until {LEARN_MIN})")
        return 0
    model = fit_model(ratings)
    # skip the bias term; rank the rest by signed weight.
    pairs = [(FEATURE_NAMES[i], model["mean"][i])
             for i in range(N_FEATURES) if FEATURE_NAMES[i] != "bias"]
    pairs.sort(key=lambda p: p[1], reverse=True)
    likes = [(nm, w) for nm, w in pairs if w > 0.05][:6]
    dislikes = [(nm, w) for nm, w in pairs if w < -0.05][-6:]
    dislikes.sort(key=lambda p: p[1])
    print(f"learned from {n} ratings:")
    if likes:
        print("  you like:    " +
              ", ".join(f"{_humanize(nm)} +{w:.1f}" for nm, w in likes))
    if dislikes:
        print("  you dislike: " +
              ", ".join(f"{_humanize(nm)} {w:.1f}" for nm, w in dislikes))
    if not likes and not dislikes:
        print("  no strong preferences yet")
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "session":
        return cmd_session()
    if len(argv) >= 2 and argv[1] == "startup":
        return cmd_startup()
    if len(argv) >= 2 and argv[1] == "confide":
        return cmd_confide(argv[2:])
    if len(argv) >= 2 and argv[1] == "status":
        return cmd_status()
    if len(argv) >= 2 and argv[1] == "learned":
        return cmd_learned()
    if len(argv) >= 2 and argv[1] == "look":
        return cmd_look(argv[2:])
    if len(argv) >= 2 and argv[1] in LEVERS:
        return cmd_lever(argv[1], argv[2:])
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
