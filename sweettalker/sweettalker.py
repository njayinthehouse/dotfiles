#!/usr/bin/env python3
"""sweettalker — roll and rate whole terminal "looks".

A *look* is one value per lever:
  prompt      the zsh prompt (curated pool)
  font        the Alacritty font family (auto-discovered monospace)
  size        the font size
  foreground  the text colour      (Alacritty colors.primary.foreground)
  background  the window colour     (Alacritty colors.primary.background)
  palette     the 16 ANSI colours   (Alacritty colors.normal/bright.*)

You roll a whole look (or tweak one lever) and rate the whole thing 0-10. In
Stage 1 rolls are random (contrast-filtered so looks stay readable) and ratings
are just collected; the learning layer that turns those ratings into smart rolls
comes in Stage 2.

Usage:
  sweettalk look [roll|auto [on|off]|rate <0-10>|help]     # the whole look
  sweettalk <lever> [roll|help]        lever = prompt|font|size|foreground|
                                              background|palette
  sweettalk confide                    rate the current look 0-10 (interactive)
  sweettalk status                     show the current look
  sweettalk startup                    shell-start hook (roll if autoroll, apply)
"""

import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path(os.environ.get("SWEETTALKER_DATA",
                               Path.home() / ".local/share/sweettalker"))
STATE = DATA_DIR / "state.json"
PROMPT_CURRENT = DATA_DIR / "current.zsh"

ALACRITTY_DIR = Path.home() / ".config/alacritty"
ALACRITTY_CFG = ALACRITTY_DIR / "alacritty.toml"
LOOK_FILE = ALACRITTY_DIR / "sweettalker.toml"
LOOK_IMPORT = "~/.config/alacritty/sweettalker.toml"

SCALE = 10
LEVERS = ["prompt", "font", "size", "foreground", "background", "palette"]
ANSI = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
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
    "#ebdbb2", "#d8dee9", "#abb2bf", "#f8f8f2", "#cdd6f4", "#a9b1d6",
    "#e0def4", "#ffffff", "#fdf6e3",                 # light
    "#1d2021", "#282828", "#073642", "#3c3836",      # dark
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


def _ensure_import():
    ALACRITTY_DIR.mkdir(parents=True, exist_ok=True)
    text = ALACRITTY_CFG.read_text() if ALACRITTY_CFG.exists() else ""
    if "sweettalker-font.toml" in text:                 # migrate old name
        text = text.replace("sweettalker-font.toml", "sweettalker.toml")
        ALACRITTY_CFG.write_text(text)
        (ALACRITTY_DIR / "sweettalker-font.toml").unlink(missing_ok=True)
    if "sweettalker.toml" in text:
        return
    if "[general]" in text or re.search(r"^\s*import\s*=", text, re.M):
        sys.stderr.write("sweettalk: add to alacritty.toml [general] import: "
                         f'"{LOOK_IMPORT}"\n')
        return
    ALACRITTY_CFG.write_text(text + f'\n[general]\nimport = ["{LOOK_IMPORT}"]\n')


def look_toml(look):
    pal = PALETTES[look["palette"]]
    lines = ["# written by sweettalker",
             "[font]", f"size = {float(look['size'])}",
             "[font.normal]", f'family = "{look["font"]}"',
             "[colors.primary]",
             f'foreground = "{look["foreground"]}"',
             f'background = "{look["background"]}"',
             "[colors.normal]"]
    lines += [f'{n} = "{c}"' for n, c in zip(ANSI, pal["normal"])]
    lines += ["[colors.bright]"]
    lines += [f'{n} = "{c}"' for n, c in zip(ANSI, pal["bright"])]
    return "\n".join(lines) + "\n"


def ipc_args(look):
    pal = PALETTES[look["palette"]]
    args = [f'font.normal.family="{look["font"]}"',
            f'font.size={float(look["size"])}',
            f'colors.primary.foreground="{look["foreground"]}"',
            f'colors.primary.background="{look["background"]}"']
    args += [f'colors.normal.{n}="{c}"' for n, c in zip(ANSI, pal["normal"])]
    args += [f'colors.bright.{n}="{c}"' for n, c in zip(ANSI, pal["bright"])]
    return args


def apply_look(look):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_CURRENT.write_text("PROMPT=" + _squote(PROMPT_MAP[look["prompt"]]) + "\n")
    _ensure_import()
    LOOK_FILE.write_text(look_toml(look))
    if os.environ.get("SWEETTALKER_NO_IPC"):
        return
    try:
        subprocess.run(["alacritty", "msg", "config", "-w", "-1", *ipc_args(look)],
                       check=False, capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass


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


def _record_rating(state, value):
    cur = state["current"]
    state.setdefault("ratings", []).append({"look": dict(cur), "rating": value})
    save_all(state)
    print(f"    ✓ {value}/{SCALE}  ({len(state['ratings'])} ratings so far)")


def cmd_confide(args):
    state = load_all()
    cur = ensure_current(state)
    print("confide — rate this whole look (Enter to skip)\n")
    show_look(cur)
    print()
    v = _ask_rating("this look")
    if v is not None:
        _record_rating(state, v)
    return 0


def cmd_look(args):
    if not args:
        return cmd_status()
    sub = args[0]
    if sub == "roll":
        state = load_all()
        ensure_current(state)
        state["current"] = random_look()
        apply_look(state["current"])
        save_all(state)
        sys.stderr.write("sweettalk: new look\n")
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


def cmd_startup():
    state = load_all()
    ensure_current(state)
    if state.get("autoroll", True):
        state["current"] = random_look()
    apply_look(state["current"])
    save_all(state)
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "startup":
        return cmd_startup()
    if len(argv) >= 2 and argv[1] == "confide":
        return cmd_confide(argv[2:])
    if len(argv) >= 2 and argv[1] == "status":
        return cmd_status()
    if len(argv) >= 2 and argv[1] == "look":
        return cmd_look(argv[2:])
    if len(argv) >= 2 and argv[1] in LEVERS:
        return cmd_lever(argv[1], argv[2:])
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
