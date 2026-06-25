#!/usr/bin/env python3
"""sweettalker — generate, rate, and learn whole terminal "looks" (v2, RL).

A *look* is generated from scratch, not picked from curated pools. Internally a
look is a GENOME — a small bundle of perceptual knobs (background lightness/hue,
a target foreground contrast, a palette hue-shift/chroma, a font, and a
compositional prompt) — which DECODE turns into the concrete values the terminal
consumes: a foreground/background hex, 16 ANSI colours, a font, and a
zsh PROMPT string. Colours are sampled in OKLCH (a perceptual space) so random
genomes still look designed, and the foreground's lightness is *solved* to hit a
target WCAG contrast instead of rejection-sampled.

Learning is preference-based (RLHF-style). You compare two looks (`look duel`);
each comparison trains a Bradley-Terry reward model — a Bayesian logistic
regression over continuous features of a look — whose utility r(look) both ranks
looks and, via percentile, *guesses a look's 0-10 rating* (`look guess`). The
inverse-Hessian (Laplace) covariance gives a per-look uncertainty.

Rolling is a sliding explore/exploit policy. Each roll samples K candidate
genomes, scores them, and picks one by softmax over standardized utility:
  EXPLOIT  favours high predicted utility  (high score => high probability)
  EXPLORE  flips the sign (low predicted favoured) AND adds an uncertainty bonus
The exploration rate (probability a roll is EXPLORE) auto-decays as comparisons
accumulate, can be pinned manually (`look explore <0..1>`), or forced per roll
(`look roll explore|exploit`).

Usage:
  sweettalk look [roll [explore|exploit]|duel|refine|auto [on|off]
                  |explore [0..1|auto]|help]
  sweettalk <lever> [roll [explore|exploit]|duel|refine|help]
                                  lever = prompt|font|foreground|
                                          background|palette
  sweettalk guess                 predict the current look's 0-10 rating

  duel   — rate the current look against the most *informative* opponent
  refine — rate it against a small local *tweak* (polish a look you like)
  Both vary the whole look, or only one lever with `<lever> duel|refine`.
  sweettalk status                show the current look + its predicted rating
  sweettalk learned               show what the model has learned you like

  Font *size* is not a lever: control it yourself with the terminal's zoom
  (Ctrl+= / Ctrl+- in st). sweettalker only sets the font *family*, leaving
  whatever size you've zoomed to untouched.
  sweettalk session               startx-session hook (roll if autoroll, apply)
  sweettalk startup               shell-start hook (apply current look)
"""

import json
import math
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
LEVERS = ["prompt", "font", "foreground", "background", "palette"]
MIN_CONTRAST = 4.5          # WCAG AA-ish floor we always construct toward


# ============================================================ OKLab colour math
# sRGB <-> linear <-> OKLab <-> OKLCH, all pure Python. OKLab (Ottosson 2020) is
# perceptually uniform, so sampling L/C/H gives colours that look deliberate; its
# cylindrical form OKLCH exposes Lightness, Chroma, Hue as independent dials.

def _cbrt(x):
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def _srgb_to_linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c):
    c = max(0.0, min(1.0, c))                      # gamut clip
    c = c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1.0 / 2.4) - 0.055
    return max(0, min(255, round(c * 255)))


def hex_to_oklab(hexs):
    h = hexs.lstrip("#")
    r, g, b = (_srgb_to_linear(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def oklab_to_hex(L, a, b):
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return "#%02x%02x%02x" % (_linear_to_srgb(r), _linear_to_srgb(g),
                              _linear_to_srgb(bb))


def oklch_to_hex(L, C, H):
    rad = math.radians(H)
    return oklab_to_hex(L, C * math.cos(rad), C * math.sin(rad))


def hex_to_oklch(hexs):
    L, a, b = hex_to_oklab(hexs)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360.0


# ----- WCAG luminance + contrast (the legibility currency) -------------------
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


def solve_fg_lightness(bg_hex, hue, chroma, target):
    """OKLCH lightness for a foreground at (hue, chroma) that hits `target`
    WCAG contrast against bg. Contrast is monotonic in |fg_L - bg_L|, so we
    binary-search L on the legible side of the background and take the closest
    reachable value if the target is out of gamut."""
    bg_L = hex_to_oklch(bg_hex)[0]
    lo, hi = (bg_L, 1.0) if bg_L < 0.5 else (0.0, bg_L)   # away from the bg
    best, best_err = None, None
    for _ in range(28):
        mid = (lo + hi) / 2
        c = contrast(oklch_to_hex(mid, chroma, hue), bg_hex)
        err = abs(c - target)
        if best_err is None or err < best_err:
            best, best_err = mid, err
        if c < target:                                    # need more separation
            lo, hi = (mid, hi) if bg_L < 0.5 else (lo, mid)
        else:
            lo, hi = (lo, mid) if bg_L < 0.5 else (mid, hi)
    return best


# ==================================================================== font lever
# Family is discrete (must be installed). Size is *not* a genome knob — it's left
# to the terminal's own zoom (Ctrl+= / Ctrl+- in st). Attribute tables
# (ligature/bitmap/width/style) feed the reward model's features.
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
LIGATURE_FONTS = (
    "firacode", "fira code", "cascadia", "caskaydia", "jetbrains", "victor",
    "hasklug", "hasklig", "monoid", "lilex", "d2coding", "iosevka", "commit",
    "geistmono", "geist mono", "monaspice", "recmono", "comicshanns", "intone",
    "zedmono", "zed mono", "fantasque", "blexmono", "code new roman",
)
BITMAP_FONTS = (
    "gohufont", "proggy", "terminus", "terminess", "profont", "bigblueterm",
    "bitstrom", "3270", "shuretech", "envycoder",
)
WIDTH_WIDE = ("wide", "expanded", "extended", "extd", "exp")
WIDTH_NARROW = ("cond", "narrow", "compress")
STYLE_SLAB = ("slab",)
STYLE_HAND = ("comic", "fantasque", "opendyslexic", "monofur", "shanns",
              "casual", "handwrit")


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


_FONT_CACHE = None


def font_values():
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
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
    _FONT_CACHE = arms or ["monospace"]
    return _FONT_CACHE


def _font_attrs(family):
    low = family.lower()
    return {
        "ligatures": any(s in low for s in LIGATURE_FONTS),
        "bitmap": any(s in low for s in BITMAP_FONTS),
        "wide": any(s in low for s in WIDTH_WIDE),
        "narrow": any(s in low for s in WIDTH_NARROW),
        "slab": any(s in low for s in STYLE_SLAB),
        "handwritten": any(s in low for s in STYLE_HAND),
    }


# ============================================================= prompt (compositional)
# A prompt is composed from segments + a trailing glyph, each coloured by an ANSI
# index (so the prompt automatically tracks the generated palette). The genome
# stores which segments, their order is canonical, plus glyph and layout.
SEG_POOL = ["time", "user", "host", "path", "git", "load"]
SEG_ORDER = ["time", "user", "host", "path", "git", "load"]   # canonical render order
GLYPHS = ["❯", "λ", "$", "%", "»", "›", "★", "➜", "#", "→"]
GLYPH_CLASS = {
    "❯": "chevron", "➜": "chevron", "→": "chevron", "›": "angle-quote",
    "»": "guillemet", "λ": "lambda", "$": "dollar", "#": "hash",
    "%": "hash", "★": "star",
}
GLYPH_CLASSES = ["chevron", "angle-quote", "guillemet", "lambda", "dollar",
                 "hash", "star", "face"]
ACCENT_INDICES = [1, 2, 3, 4, 5, 6, 8, 12, 14]   # ANSI indices that read as accents

G = "${vcs_info_msg_0_}"      # git segment (vcs_info, configured in sweettalker.zsh)


def _seg_zsh(seg, color):
    c = "%%F{%d}" % color
    if seg == "time":
        return c + "%T%f"
    if seg == "user":
        return c + "%n%f"
    if seg == "host":
        return c + "%m%f"
    if seg == "path":
        return c + "%~%f"
    if seg == "git":
        return G
    if seg == "load":
        return c + '$(cut -d" " -f1 /proc/loadavg)%f'
    return ""


# ==================================================================== the genome
def _rand_bg():
    """Background in OKLCH: a dark theme (75%) or a light one, low chroma."""
    if random.random() < 0.75:
        L = random.uniform(0.10, 0.32)
    else:
        L = random.uniform(0.86, 0.97)
    return {"L": L, "C": random.uniform(0.0, 0.045),
            "H": random.uniform(0.0, 360.0)}


def _rand_prompt():
    segs = ["path"] if random.random() < 0.9 else []
    for s in SEG_POOL:
        if s == "path":
            continue
        on = {"git": 0.6, "time": 0.35, "host": 0.25, "user": 0.2,
              "load": 0.15}.get(s, 0.2)
        if random.random() < on:
            segs.append(s)
    glyph = random.choice(GLYPHS)
    return {
        "segments": segs,
        "glyph": glyph,
        "status_glyph": glyph not in ("$", "#", "%") and random.random() < 0.4,
        "two_line": random.random() < 0.35,
        "seg_colors": {s: random.choice(ACCENT_INDICES) for s in SEG_POOL},
        "glyph_color": random.choice(ACCENT_INDICES),
    }


def random_genome():
    return {
        "bg": _rand_bg(),
        "fg": {"H": random.uniform(0.0, 360.0),
               "C": random.uniform(0.0, 0.11),
               "contrast": random.uniform(MIN_CONTRAST, 13.0)},
        "palette": {"hue_shift": random.uniform(-35.0, 35.0),
                    "chroma": random.uniform(0.07, 0.17),
                    "normal_L": random.uniform(0.45, 0.66),
                    "bright_dL": random.uniform(0.08, 0.22)},
        "font": {"family": random.choice(font_values())},
        "prompt": _rand_prompt(),
    }


def mutate_genome(genome, lever):
    """Return a copy with just one lever re-rolled (others kept)."""
    g = json.loads(json.dumps(genome))      # deep copy
    fresh = random_genome()
    if lever == "background":
        g["bg"] = fresh["bg"]
    elif lever == "foreground":
        g["fg"] = fresh["fg"]
    elif lever == "palette":
        g["palette"] = fresh["palette"]
    elif lever == "font":
        g["font"]["family"] = fresh["font"]["family"]
    elif lever == "prompt":
        g["prompt"] = fresh["prompt"]
    return g


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _perturb_prompt(p):
    """One small, in-place change to a prompt: toggle a segment, swap the glyph,
    flip the layout, or recolour something."""
    r = random.random()
    if r < 0.35:
        s = random.choice(SEG_POOL)
        if s in p["segments"] and s != "path":
            p["segments"].remove(s)
        elif s not in p["segments"]:
            p["segments"].append(s)
    elif r < 0.6:
        p["glyph"] = random.choice([g for g in GLYPHS if g != p["glyph"]])
    elif r < 0.8:
        p["two_line"] = not p["two_line"]
    else:
        if p["segments"]:
            p["seg_colors"][random.choice(p["segments"])] = random.choice(ACCENT_INDICES)
        p["glyph_color"] = random.choice(ACCENT_INDICES)


def perturb_genome(genome, lever=None):
    """A small local *tweak* of a genome (for `refine`), not a fresh re-roll.
    Continuous knobs get gaussian nudges (clamped to valid ranges); discrete ones
    (font family, prompt shape) flip occasionally. With a lever, only that lever
    is nudged; without one, colours always shift slightly and the prompt sometimes
    does — font is left alone in whole-look refine since a family swap isn't subtle."""
    g = json.loads(json.dumps(genome))
    whole = lever is None

    def do(name):
        return whole or lever == name

    if do("background"):
        b = g["bg"]
        b["L"] = _clamp(b["L"] + random.gauss(0, 0.04), 0.04, 0.98)
        b["C"] = _clamp(b["C"] + random.gauss(0, 0.01), 0.0, 0.06)
        b["H"] = (b["H"] + random.gauss(0, 12)) % 360
    if do("foreground"):
        f = g["fg"]
        f["H"] = (f["H"] + random.gauss(0, 15)) % 360
        f["C"] = _clamp(f["C"] + random.gauss(0, 0.015), 0.0, 0.13)
        f["contrast"] = _clamp(f["contrast"] + random.gauss(0, 0.8), MIN_CONTRAST, 16.0)
    if do("palette"):
        p = g["palette"]
        p["hue_shift"] = _clamp(p["hue_shift"] + random.gauss(0, 6), -45, 45)
        p["chroma"] = _clamp(p["chroma"] + random.gauss(0, 0.015), 0.05, 0.20)
        p["normal_L"] = _clamp(p["normal_L"] + random.gauss(0, 0.03), 0.40, 0.70)
        p["bright_dL"] = _clamp(p["bright_dL"] + random.gauss(0, 0.03), 0.05, 0.25)
    if lever == "font":                              # can't nudge a family; pick a neighbour
        fams = [x for x in font_values() if x != g["font"]["family"]]
        if fams:
            g["font"]["family"] = random.choice(fams)
    if lever == "prompt" or (whole and random.random() < 0.3):
        _perturb_prompt(g["prompt"])
    return g


# ==================================================================== decode
# Canonical ANSI hues (OKLCH degrees) so generated palettes keep red≈red etc.,
# while the genome's hue_shift/chroma/lightness give each palette its character.
ANSI_HUES = {1: 28, 2: 142, 3: 95, 4: 256, 5: 330, 6: 195}   # r g y b m c


def _decode_palette(genome, bg_hex, fg_hex):
    p = genome["palette"]
    bg_L = hex_to_oklch(bg_hex)[0]
    dark = bg_L < 0.5
    shift, C = p["hue_shift"], p["chroma"]
    nL, bL = p["normal_L"], min(0.95, p["normal_L"] + p["bright_dL"])
    # 0/8 = "black", 7/15 = "white": greys anchored to the background end.
    g0 = bg_L + (0.10 if dark else -0.10)
    g8 = bg_L + (0.28 if dark else -0.22)
    g7 = bg_L + (0.62 if dark else -0.45)
    g15 = bg_L + (0.78 if dark else -0.60)
    normal, bright = [None] * 8, [None] * 8
    normal[0] = oklch_to_hex(max(0.0, min(1.0, g0)), 0.01, 0.0)
    bright[0] = oklch_to_hex(max(0.0, min(1.0, g8)), 0.01, 0.0)
    normal[7] = oklch_to_hex(max(0.0, min(1.0, g7)), 0.01, 0.0)
    bright[7] = oklch_to_hex(max(0.0, min(1.0, g15)), 0.012, 0.0)
    for i in range(1, 7):
        hue = (ANSI_HUES[i] + shift) % 360
        normal[i] = oklch_to_hex(nL, C, hue)
        bright[i] = oklch_to_hex(bL, min(0.22, C * 1.12), hue)
    return normal, bright


def _decode_prompt(genome):
    p = genome["prompt"]
    parts = [_seg_zsh(s, p["seg_colors"].get(s, 4))
             for s in SEG_ORDER if s in p["segments"]]
    body = " ".join(x for x in parts if x)
    sep = "\n" if p["two_line"] else (" " if body else "")
    glyph = p["glyph"]
    if p["status_glyph"]:
        tail = "%%(?.%%F{2}%s.%%F{1}%s)%%f " % (glyph, glyph)
    else:
        tail = "%%F{%d}%s%%f " % (p["glyph_color"], glyph)
    return (body + sep + tail) if body else tail


def decode(genome):
    """Genome -> concrete look. Deterministic. The returned dict carries both the
    terminal-ready values and the prompt metadata the feature extractor needs."""
    bg = oklch_to_hex(genome["bg"]["L"], genome["bg"]["C"], genome["bg"]["H"])
    fg_L = solve_fg_lightness(bg, genome["fg"]["H"], genome["fg"]["C"],
                              genome["fg"]["contrast"])
    fg = oklch_to_hex(fg_L, genome["fg"]["C"], genome["fg"]["H"])
    normal, bright = _decode_palette(genome, bg, fg)
    return {
        "fg": fg, "bg": bg, "ansi": normal + bright,
        "font": genome["font"]["family"],
        "prompt_str": _decode_prompt(genome),
        "prompt_meta": dict(genome["prompt"]),
    }


def show_look(look, genome=None):
    pm = look["prompt_meta"]
    segs = ",".join(s for s in SEG_ORDER if s in pm["segments"]) or "(glyph only)"
    print(f"  prompt     {segs}  {pm['glyph']}"
          f"{'  2-line' if pm.get('two_line') else ''}"
          f"{'  status' if pm.get('status_glyph') else ''}")
    print(f"  font       {look['font']}")
    print(f"  foreground {look['fg']}   (contrast {contrast(look['fg'], look['bg']):.1f})")
    print(f"  background {look['bg']}")
    print(f"  palette    {' '.join(look['ansi'][1:7])}")


# ================================================================== features
# A continuous feature vector over a *decoded* look. Used for both genomes
# (decode first) and migrated legacy looks (already concrete). No bias term:
# the reward model is trained on look-vs-look differences, where constants
# cancel, so a bias would never receive gradient.

def _hue_xy(L, C, H, weight=1.0):
    """Hue as (sin,cos) scaled by chroma, so near-grey colours contribute ~0
    hue signal instead of a phantom direction."""
    rad = math.radians(H)
    return [weight * C * math.cos(rad), weight * C * math.sin(rad)]


def feature_vector(look):
    bgL, bgC, bgH = hex_to_oklch(look["bg"])
    fgL, fgC, fgH = hex_to_oklch(look["fg"])
    f = [bgL, bgC, 1.0 if bgL < 0.5 else 0.0, fgL, fgC,
         contrast(look["fg"], look["bg"]) / 21.0]
    f += _hue_xy(bgL, bgC, bgH)
    f += _hue_xy(fgL, fgC, fgH)
    # palette: mean lightness/chroma of the 6 chromatic normals + bright gap
    chrom = [hex_to_oklch(look["ansi"][i]) for i in range(1, 7)]
    bchrom = [hex_to_oklch(look["ansi"][i]) for i in range(9, 15)]
    f += [sum(c[0] for c in chrom) / 6.0,
          sum(c[1] for c in chrom) / 6.0,
          (sum(c[0] for c in bchrom) - sum(c[0] for c in chrom)) / 6.0]
    a = _font_attrs(look["font"])
    f += [1.0 if a[k] else 0.0 for k in
          ("ligatures", "bitmap", "wide", "narrow", "slab", "handwritten")]
    pm = look["prompt_meta"]
    segs = pm.get("segments", [])
    f += [0.0 if pm.get("two_line") else 1.0,        # one_line
          1.0 if "git" in segs else 0.0,
          1.0 if "time" in segs else 0.0,
          1.0 if "load" in segs else 0.0,
          1.0 if "host" in segs else 0.0,
          1.0 if "user" in segs else 0.0,
          len(segs) / 6.0,
          1.0 if pm.get("status_glyph") else 0.0]
    gclass = "face" if pm.get("status_glyph") else GLYPH_CLASS.get(pm.get("glyph"), "chevron")
    f += [1.0 if gclass == c else 0.0 for c in GLYPH_CLASSES]
    return f


FEATURE_NAMES = (
    ["bg.L", "bg.C", "bg.dark", "fg.L", "fg.C", "contrast",
     "bg.hue.x", "bg.hue.y", "fg.hue.x", "fg.hue.y",
     "pal.L", "pal.C", "pal.bright_gap",
     "font.ligatures", "font.bitmap", "font.wide", "font.narrow",
     "font.slab", "font.handwritten",
     "prompt.one_line", "prompt.git", "prompt.time", "prompt.load",
     "prompt.host", "prompt.user", "prompt.n_segs", "prompt.status_glyph"]
    + [f"glyph.{c}" for c in GLYPH_CLASSES])
N_FEATURES = len(FEATURE_NAMES)
assert len(feature_vector(decode(random_genome()))) == N_FEATURES, "feature/name mismatch"


def feat(obj):
    """Feature vector for a genome (decode first) or an already-concrete look."""
    return feature_vector(decode(obj) if isinstance(obj.get("bg"), dict) else obj)


# ----------------------------------------------------- pure-Python linear algebra
def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _outer_add(M, v, scale):
    for i, vi in enumerate(v):
        s = scale * vi
        if s == 0.0:
            continue
        row = M[i]
        for j, vj in enumerate(v):
            row[j] += s * vj


def _cholesky(A):
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
    """Solve (L Lᵀ) x = b for lower-triangular L."""
    n = len(L)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


def _sigmoid(z):
    if z < -35:
        return 0.0
    if z > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


# ============================================ Bradley-Terry reward model (Laplace)
# Each comparison (A vs B, winner) constrains a utility r(look)=wᵀφ via
# P(A≻B)=σ(wᵀ(φA−φB)). MAP w by Newton/IRLS on the L2-regularised logistic loss;
# the inverse Hessian at the optimum is the Laplace posterior covariance, which
# yields a per-look utility variance for the explore bonus. Pure stdlib.

PREF_MIN = 6                 # comparisons before the model drives rolls
RIDGE_LAMBDA = 1.0
NEWTON_ITERS = 30


def _comparison_diffs(comparisons):
    rows = []
    for c in comparisons:
        w = c.get("winner")
        if w not in ("a", "b", "tie"):
            continue
        try:
            d = [x - y for x, y in zip(feat(c["a"]), feat(c["b"]))]
        except (KeyError, ValueError, TypeError):
            continue
        y = 1.0 if w == "a" else (0.0 if w == "b" else 0.5)
        rows.append((d, y))
    return rows


def fit_model(comparisons):
    """Newton-fit the Bradley-Terry posterior. Returns dict(w, L_H, n) or None
    below PREF_MIN usable comparisons."""
    rows = _comparison_diffs(comparisons)
    if len(rows) < PREF_MIN:
        return None
    n = N_FEATURES
    w = [0.0] * n
    L_H = None
    for _ in range(NEWTON_ITERS):
        grad = [RIDGE_LAMBDA * wi for wi in w]
        H = [[0.0] * n for _ in range(n)]
        for i in range(n):
            H[i][i] = RIDGE_LAMBDA
        for d, y in rows:
            p = _sigmoid(_dot(w, d))
            r = p - y
            for i, di in enumerate(d):
                grad[i] += r * di
            _outer_add(H, d, p * (1.0 - p))
        L_H = _cholesky(H)
        step = _solve_chol(L_H, grad)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return {"w": w, "L_H": L_H, "n": len(rows)}


def utility(model, look_or_genome):
    return _dot(model["w"], feat(look_or_genome))


def utility_var(model, look_or_genome):
    """Posterior variance of the utility: xᵀ H⁻¹ x (Laplace covariance)."""
    x = feat(look_or_genome)
    return max(0.0, _dot(x, _solve_chol(model["L_H"], x)))


_REF_GENOMES = None


def _ref_genomes(n=256):
    """A fixed reference set of looks, the yardstick percentile ratings are
    measured against. Seeded and cached so a look's predicted rating is stable
    across calls; generated without disturbing the global RNG that drives rolls."""
    global _REF_GENOMES
    if _REF_GENOMES is None:
        saved = random.getstate()
        random.seed(0x5EE77A1C)
        _REF_GENOMES = [random_genome() for _ in range(n)]
        random.setstate(saved)
    return _REF_GENOMES


def predict_rating(model, look_or_genome, population=None):
    """Calibrated 0-SCALE guess: the look's percentile of utility among the fixed
    reference set. Returns (rating, stddev_of_utility)."""
    if population is None:
        population = [utility(model, g) for g in _ref_genomes()]
    u = utility(model, look_or_genome)
    below = sum(1 for pu in population if pu < u)
    rating = SCALE * below / max(1, len(population))
    return rating, utility_var(model, look_or_genome) ** 0.5


# ====================================== legacy migration (old 0-10 ratings -> prefs)
# The old engine stored {look,rating} with discrete pool-picks. Each pair of old
# looks whose ratings differ by >= 2 becomes one synthetic comparison, so v2
# starts from your existing taste instead of cold. Runs once (state['migrated']).
OLD_PALETTES = {
    "gruvbox": ["#282828", "#cc241d", "#98971a", "#d79921", "#458588", "#b16286",
                "#689d6a", "#a89984", "#928374", "#fb4934", "#b8bb26", "#fabd2f",
                "#83a598", "#d3869b", "#8ec07c", "#ebdbb2"],
    "nord": ["#3b4252", "#bf616a", "#a3be8c", "#ebcb8b", "#81a1c1", "#b48ead",
             "#88c0d0", "#e5e9f0", "#4c566a", "#bf616a", "#a3be8c", "#ebcb8b",
             "#81a1c1", "#b48ead", "#8fbcbb", "#eceff4"],
    "dracula": ["#21222c", "#ff5555", "#50fa7b", "#f1fa8c", "#bd93f9", "#ff79c6",
                "#8be9fd", "#f8f8f2", "#6272a4", "#ff6e6e", "#69ff94", "#ffffa5",
                "#d6acff", "#ff92df", "#a4ffff", "#ffffff"],
    "tokyo-night": ["#15161e", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7",
                    "#bb9af7", "#7dcfff", "#a9b1d6", "#414868", "#f7768e",
                    "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7", "#7dcfff", "#c0caf5"],
    "solarized": ["#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682",
                  "#2aa198", "#eee8d5", "#002b36", "#cb4b16", "#586e75", "#657b83",
                  "#839496", "#6c71c4", "#93a1a1", "#fdf6e3"],
}


def _legacy_prompt_meta(body):
    segs = []
    if "%~" in body:
        segs.append("path")
    if "${vcs_info_msg_0_}" in body:
        segs.append("git")
    if "%T" in body or "date " in body:
        segs.append("time")
    if "%m" in body:
        segs.append("host")
    if "%n" in body:
        segs.append("user")
    if "loadavg" in body:
        segs.append("load")
    glyph = next((g for g in GLYPHS if g in body), "❯")
    return {"segments": segs, "glyph": glyph, "two_line": "\n" in body,
            "status_glyph": "%(?" in body, "seg_colors": {}, "glyph_color": 4}


def normalize_legacy_look(old):
    """Old pool-pick look -> the concrete-look shape feature_vector expects."""
    pal = OLD_PALETTES.get(old.get("palette"), OLD_PALETTES["gruvbox"])
    # The old prompt body isn't stored, only its name; rebuild meta from the name
    # via the same detectors. Unknown names degrade to a path+chevron prompt.
    body = old.get("_prompt_body", "%~ ❯ ")
    return {"fg": old["foreground"], "bg": old["background"], "ansi": list(pal),
            "font": old["font"],
            "prompt_meta": _legacy_prompt_meta(body)}


# Old prompt bodies, by name, so legacy looks featurize with real prompt shape.
OLD_PROMPT_BODIES = {
    "minimal": "❯ ", "arrow": "%~ ❯ ", "arrow-status": "%~ %(?.❯.❯) ",
    "classic": "%n@%m %~ ", "classic-color": "%n@%m:%~ ",
    "lambda": "λ %~ ", "bracket": "[%~] $ ", "dollar": "%~ $ ",
    "angle": "%~ » ", "chevron": "%~ › ", "star": "%~ ★ ", "dim": "%~ ❯ ",
    "time": "%T %~ ❯ ", "host": "%m %~ › ", "two-line": "%~\n❯ ",
    "two-line-box": "%~\n❯ ", "clock": "$(date) %~ ❯ ",
    "status-face": "%~ %(?.^.x) ", "loadavg": "loadavg %~ ❯ ",
}


def migrate_legacy(state):
    """Build synthetic comparisons from old {look,rating} entries (once)."""
    if state.get("migrated") or not state.get("ratings"):
        return
    rated = []
    for e in state["ratings"]:
        look, r = e.get("look"), e.get("rating")
        if not look or r is None:
            continue
        look = dict(look)
        look["_prompt_body"] = OLD_PROMPT_BODIES.get(look.get("prompt"), "%~ ❯ ")
        try:
            rated.append((normalize_legacy_look(look), float(r)))
        except (KeyError, ValueError):
            continue
    comps = state.setdefault("comparisons", [])
    added = 0
    for i in range(len(rated)):
        for j in range(i + 1, len(rated)):
            (la, ra), (lb, rb) = rated[i], rated[j]
            if abs(ra - rb) < 2:
                continue
            comps.append({"a": la, "b": lb, "winner": "a" if ra > rb else "b",
                          "source": "legacy"})
            added += 1
    state["migrated"] = True
    return added


# ================================================ sliding explore/exploit policy
# Each roll samples K candidate genomes and picks one by softmax over standardized
# utility. EXPLOIT weights high predicted utility (high score -> high probability);
# EXPLORE flips that sign (low predicted favoured) and adds an uncertainty bonus.
# The exploration RATE (probability a roll is EXPLORE) auto-decays with data,
# can be pinned to a float, or forced per roll.

K_CANDIDATES = 48
EXPLORE_RATE0 = 0.9
EXPLORE_DECAY = 0.985
EXPLORE_MIN = 0.10
EXPLORE_GAMMA = 1.0         # uncertainty bonus weight in EXPLORE


def exploration_rate(state):
    """The slider value in [0,1]. A stored float pins it; otherwise it auto-decays
    with the number of comparisons gathered."""
    r = state.get("explore_rate")
    if isinstance(r, (int, float)):
        return max(0.0, min(1.0, float(r)))
    n = len(state.get("comparisons", []))
    return max(EXPLORE_MIN, EXPLORE_RATE0 * (EXPLORE_DECAY ** n))


def _standardize(xs):
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    sd = var ** 0.5 or 1.0
    return [(x - m) / sd for x in xs]


def _softmax_pick(logits):
    hi = max(logits)
    exps = [math.exp(l - hi) for l in logits]
    total = sum(exps)
    r = random.random() * total
    acc = 0.0
    for i, e in enumerate(exps):
        acc += e
        if r <= acc:
            return i
    return len(logits) - 1


def policy_roll(state, model, forced=None, lever=None):
    """Pick a genome. forced ∈ {explore, exploit, None}. lever=None rolls the
    whole look; otherwise only that lever varies off the current genome.
    Returns (genome, mode, beta)."""
    base = state.get("genome") or random_genome()
    if lever:
        cands = [mutate_genome(base, lever) for _ in range(K_CANDIDATES)]
    else:
        cands = [random_genome() for _ in range(K_CANDIDATES)]
    if model is None:                       # cold start: uniform random
        return random.choice(cands), "random", 0.0
    explore = (forced == "explore") if forced in ("explore", "exploit") \
        else (random.random() < exploration_rate(state))
    mode = "explore" if explore else "exploit"
    us = [utility(model, c) for c in cands]
    zu = _standardize(us)
    beta = min(3.0, 1.0 + 0.03 * model["n"])
    d = -1.0 if explore else 1.0
    if explore:
        zs = _standardize([utility_var(model, c) ** 0.5 for c in cands])
        logits = [beta * (d * u + EXPLORE_GAMMA * s) for u, s in zip(zu, zs)]
    else:
        logits = [beta * d * u for u in zu]
    return cands[_softmax_pick(logits)], mode, beta


def informative_opponent(state, model, lever=None):
    """Active-learning opponent for a `duel`: from a candidate pool, the look whose
    duel outcome the model is *least able to predict* — utility closest to the
    current look (so P(win)≈0.5, a coin flip) and high posterior uncertainty about
    the difference. That comparison adds the most information per rating."""
    base = state.get("genome") or random_genome()
    cands = ([mutate_genome(base, lever) for _ in range(K_CANDIDATES)] if lever
             else [random_genome() for _ in range(K_CANDIDATES)])
    if model is None:
        return random.choice(cands)
    fa = feat(base)
    u_a = _dot(model["w"], fa)
    rows = []
    for c in cands:
        d = [x - y for x, y in zip(fa, feat(c))]
        m = u_a - _dot(model["w"], feat(c))          # logit: P(current wins)=σ(m)
        s = max(0.0, _dot(d, _solve_chol(model["L_H"], d))) ** 0.5
        rows.append((c, m, s))
    zs = _standardize([s for _, _, s in rows])
    best, best_score = None, None
    for (c, m, _), z in zip(rows, zs):
        p = _sigmoid(m)
        score = (1.0 - abs(2.0 * p - 1.0)) + 0.3 * z   # coin-flip + uncertainty
        if best_score is None or score > best_score:
            best, best_score = c, score
    return best


# ===================================================================== applying
def _squote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _st_tty():
    """Path of the pts the outer st shares with the session neovim. sweettalk
    runs inside an nvim :terminal, so its stdout reaches nvim, not st; $NVIM names
    the session neovim whose stdout fd is st's side of the pts, where OSC escapes
    are read and applied by st itself."""
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
    """OSC escapes painting a look onto st: font family (OSC 50), fg (10), bg (11),
    and the 16 ANSI colours (OSC 4;n). BEL-terminated. Pure: no I/O. No size is
    sent — st keeps whatever size you've zoomed to (Ctrl+= / Ctrl+-)."""
    seqs = [f'\033]50;{look["font"]}\007',
            f'\033]10;{look["fg"]}\007',
            f'\033]11;{look["bg"]}\007']
    seqs += [f'\033]4;{i};{c}\007' for i, c in enumerate(look["ansi"])]
    return "".join(seqs)


def st_apply(look):
    """Paint colour + font onto the live st (best-effort). No-op unless $NVIM is
    set and IPC isn't suppressed. st marks every cell dirty on a colour change and
    reloads on a font change, so every pane recolours/reflows with no per-nvim push."""
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
    """Write the prompt to current.zsh (re-sourced live by the shell via the zsh
    precmd mtime hook) and paint colours/font onto st."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_CURRENT.write_text("PROMPT=" + _squote(look["prompt_str"]) + "\n")
    st_apply(look)


# ===================================================================== state
def load_all():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_all(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def ensure_current(state):
    """Guarantee state['genome'] exists and references an installed font."""
    g = state.get("genome")
    if not g or "bg" not in g or not isinstance(g.get("bg"), dict):
        g = random_genome()
        state["genome"] = g
    if g["font"]["family"] not in font_values():
        g["font"]["family"] = random.choice(font_values())
    return g


def _model(state):
    return fit_model(state.get("comparisons", []))


def _rating_line(model, genome):
    if not model:
        return ""
    r, sd = predict_rating(model, genome)
    return f"  predicted  {r:.1f}/{SCALE}  (±{sd:.1f} util)"


# ===================================================================== commands
def cmd_status():
    state = load_all()
    migrate_legacy(state)
    g = ensure_current(state)
    model = _model(state)
    nc = len(state.get("comparisons", []))
    auto = "on" if state.get("autoroll", True) else "off"
    rate = exploration_rate(state)
    pinned = isinstance(state.get("explore_rate"), (int, float))
    print(f"current look   (autoroll {auto}, {nc} comparisons, "
          f"explore {rate:.2f}{' pinned' if pinned else ' auto'})")
    show_look(decode(g))
    line = _rating_line(model, g)
    if line:
        print(line)
    elif nc < PREF_MIN:
        print(f"  predicted  (learning at {nc}/{PREF_MIN} comparisons)")
    save_all(state)
    return 0


def _roll_common(state, lever, forced):
    migrate_legacy(state)
    ensure_current(state)
    model = _model(state)
    g, mode, _ = policy_roll(state, model, forced=forced, lever=lever)
    state["genome"] = g
    look = decode(g)
    apply_look(look)
    save_all(state)
    return g, mode, model, look


def cmd_look(args):
    if not args:
        return cmd_status()
    sub = args[0]
    if sub == "roll":
        forced = args[1] if len(args) > 1 and args[1] in ("explore", "exploit") else None
        state = load_all()
        g, mode, model, look = _roll_common(state, None, forced)
        sys.stderr.write(f"sweettalk: new look ({mode})\n")
        show_look(look)
        line = _rating_line(model, g)
        if line:
            print(line)
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
    if sub == "duel":
        return cmd_duel(None, args[1:])
    if sub == "refine":
        return cmd_duel(None, args[1:], kind="refine")
    if sub == "explore":
        state = load_all()
        if len(args) < 2:
            rate = exploration_rate(state)
            pinned = isinstance(state.get("explore_rate"), (int, float))
            print(f"{rate:.2f} ({'pinned' if pinned else 'auto'})")
            return 0
        if args[1] == "auto":
            state["explore_rate"] = None
        else:
            try:
                state["explore_rate"] = max(0.0, min(1.0, float(args[1])))
            except ValueError:
                sys.stderr.write("usage: look explore <0..1|auto>\n")
                return 2
        save_all(state)
        sys.stderr.write(f"sweettalk: explore rate "
                         f"{exploration_rate(state):.2f}\n")
        return 0
    if sub == "help":
        sys.stderr.write(__doc__)
        return 0
    sys.stderr.write(__doc__)
    return 2


def cmd_lever(name, args):
    if not args:                                   # bare lever -> show value
        g = ensure_current(load_all())
        look = decode(g)
        vals = {"prompt": look["prompt_meta"], "font": look["font"],
                "foreground": look["fg"],
                "background": look["bg"], "palette": " ".join(look["ansi"][1:7])}
        print(vals[name])
        return 0
    if args[0] == "roll":
        forced = args[1] if len(args) > 1 and args[1] in ("explore", "exploit") else None
        state = load_all()
        g, mode, model, look = _roll_common(state, name, forced)
        sys.stderr.write(f"sweettalk {name}: rolled ({mode})\n")
        show_look(look)
        return 0
    if args[0] == "duel":
        return cmd_duel(name, args[1:])
    if args[0] == "refine":
        return cmd_duel(name, args[1:], kind="refine")
    if args[0] == "help":
        sys.stderr.write(__doc__)
        return 0
    sys.stderr.write(__doc__)
    return 2


def _ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        print()
        return ""


def cmd_duel(lever, args, kind="duel"):
    """Pairwise rating: current look (A) vs an opponent (B). Each is applied live;
    your pick trains the reward model and becomes the current look (winner sticks).
    With a lever, B differs from A in only that lever, so it trains just that
    lever's features. Two opponent strategies:
      duel    — the most *informative* opponent: the one whose winner the model can
                least predict (active learning).
      refine  — a small local *tweak* of the current look, for polishing a theme
                you already like."""
    state = load_all()
    migrate_legacy(state)
    g_a = ensure_current(state)
    model = _model(state)
    if kind == "refine":
        g_b = perturb_genome(g_a, lever)
    else:
        g_b = informative_opponent(state, model, lever)
    look_a, look_b = decode(g_a), decode(g_b)
    scope = f"{lever} {kind}" if lever else kind
    print(f"{scope} — two looks; pick the one you like more "
          f"(Enter at the end skips)\n")
    print("A:")
    show_look(look_a)
    apply_look(look_a)
    _ask("\n  [Enter] to see B ")
    print("\nB:")
    show_look(look_b)
    apply_look(look_b)
    choice = _ask("\n  prefer? a / b / = (tie) / Enter to skip > ").strip().lower()
    winner = {"a": "a", "b": "b", "=": "tie", "tie": "tie"}.get(choice)
    if winner is None:
        apply_look(look_a)                         # restore the original
        state["genome"] = g_a
        save_all(state)
        print("    skipped — nothing recorded")
        return 0
    entry = {"a": g_a, "b": g_b, "winner": winner, "kind": kind}
    if lever:
        entry["lever"] = lever
    state.setdefault("comparisons", []).append(entry)
    keep = g_b if winner == "b" else g_a
    state["genome"] = keep
    apply_look(decode(keep))
    save_all(state)
    nc = len(state["comparisons"])
    print(f"    ✓ recorded ({nc} comparisons). kept look "
          f"{'B' if winner == 'b' else 'A'}.")
    return 0


def cmd_guess():
    state = load_all()
    migrate_legacy(state)
    g = ensure_current(state)
    model = _model(state)
    nc = len(state.get("comparisons", []))
    if not model:
        print(f"can't guess yet — {nc}/{PREF_MIN} comparisons "
              f"(run `look duel` to add some)")
        return 0
    r, sd = predict_rating(model, g)
    pct = int(round(r * 10))
    print(f"predicted rating: {r:.1f}/{SCALE}  "
          f"(~{pct}th percentile of your taste, ±{sd:.1f} util)")
    return 0


def _humanize(name):
    pretty = {
        "bg.L": "lighter background", "bg.dark": "dark background",
        "bg.C": "saturated background", "fg.L": "lighter foreground",
        "fg.C": "saturated foreground", "contrast": "high contrast",
        "pal.L": "lighter palette", "pal.C": "vivid palette",
        "pal.bright_gap": "punchy bright colours",
        "font.ligatures": "ligature fonts", "font.bitmap": "bitmap fonts",
        "font.wide": "wide fonts", "font.narrow": "narrow fonts",
        "font.slab": "slab fonts", "font.handwritten": "handwritten fonts",
        "prompt.one_line": "one-line prompt",
        "prompt.git": "git in prompt", "prompt.time": "time in prompt",
        "prompt.load": "load in prompt", "prompt.host": "host in prompt",
        "prompt.user": "user in prompt", "prompt.n_segs": "busy prompt",
        "prompt.status_glyph": "status-coloured glyph",
    }
    if name in pretty:
        return pretty[name]
    if name.startswith("glyph."):
        return name.split(".")[-1] + " glyph"
    if name.endswith(".hue.x") or name.endswith(".hue.y"):
        return name.replace(".hue.x", " warm/cool hue").replace(".hue.y", " hue")
    return name


def cmd_learned():
    state = load_all()
    migrate_legacy(state)
    model = _model(state)
    nc = len(state.get("comparisons", []))
    if not model:
        print(f"learning off — {nc}/{PREF_MIN} comparisons "
              f"(rolls are random until {PREF_MIN})")
        return 0
    pairs = sorted(zip(FEATURE_NAMES, model["w"]), key=lambda p: p[1], reverse=True)
    likes = [(n, w) for n, w in pairs if w > 0.15][:6]
    dislikes = sorted([(n, w) for n, w in pairs if w < -0.15], key=lambda p: p[1])[:6]
    print(f"learned from {model['n']} comparisons:")
    if likes:
        print("  you like:    " +
              ", ".join(f"{_humanize(n)} +{w:.1f}" for n, w in likes))
    if dislikes:
        print("  you dislike: " +
              ", ".join(f"{_humanize(n)} {w:.1f}" for n, w in dislikes))
    if not likes and not dislikes:
        print("  no strong preferences yet")
    return 0


def cmd_session():
    """startx-session hook: roll a fresh look once per session if autoroll is on,
    then apply. Every shell then just applies this look via `startup`."""
    state = load_all()
    migrate_legacy(state)
    ensure_current(state)
    if state.get("autoroll", True):
        model = _model(state)
        g, _, _ = policy_roll(state, model)
        state["genome"] = g
    apply_look(decode(state["genome"]))
    save_all(state)
    return 0


def cmd_startup():
    """shell-start hook: apply the session's current look, never roll."""
    state = load_all()
    ensure_current(state)
    apply_look(decode(state["genome"]))
    save_all(state)
    return 0


def main(argv):
    cmds = {"session": cmd_session, "startup": cmd_startup, "status": cmd_status,
            "learned": cmd_learned, "guess": cmd_guess}
    if len(argv) >= 2 and argv[1] in cmds:
        return cmds[argv[1]]()
    if len(argv) >= 2 and argv[1] == "look":
        return cmd_look(argv[2:])
    if len(argv) >= 2 and argv[1] in LEVERS:
        return cmd_lever(argv[1], argv[2:])
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
