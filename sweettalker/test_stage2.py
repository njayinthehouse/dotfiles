#!/usr/bin/env python3
"""Sandbox test for sweettalker Stage 2 (the learning layer).

Never touches the real terminal or real state: SWEETTALKER_NO_IPC=1 plus a temp
HOME and SWEETTALKER_DATA are set BEFORE sweettalker is imported. Feeds synthetic
ratings encoding a known preference and asserts the model recovers it.

Run:  python3 test_stage2.py
"""
import os
import random
import tempfile

# --- isolate everything before importing the module under test ---------------
_TMP = tempfile.mkdtemp(prefix="sweettalker-test-")
os.environ["SWEETTALKER_NO_IPC"] = "1"
os.environ["HOME"] = _TMP
os.environ["SWEETTALKER_DATA"] = os.path.join(_TMP, "data")

import importlib  # noqa: E402
import sweettalker as s  # noqa: E402
importlib.reload(s)      # re-evaluate module-level paths with the temp env

FAILS = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


# --- a known preference: like two-line ∧ ligature-font ∧ dark bg --------------
LIG_FONT = "FiraCode Nerd Font Mono"      # ligatures per the curated table
PLAIN_FONT = "Terminus"                   # bitmap, no ligatures
# Use several backgrounds so dark-vs-light is carried by luminance, not a single
# collinear hue bucket — lets the linear weight on bg.luminance be identifiable.
DARK_BGS = ["#1d2021", "#0d1117", "#191724", "#1a1b26"]
LIGHT_BGS = ["#fbf1c7", "#eee8d5", "#f5f5f5", "#fdf6e3"]
DARK_FG = "#1d2021"
LIGHT_FG = "#ebdbb2"


def preferred(look):
    """Score the synthetic preference (higher = liked)."""
    f, fn = s._build_features(look)
    fmap = dict(zip(fn, f))
    two_line = fmap["prompt.one_line"] < 0.5
    lig = fmap["font.ligatures"] > 0.5
    dark_bg = fmap["bg.luminance"] < 0.1
    score = 1.0
    score += 3.0 if two_line else 0.0
    score += 3.0 if lig else 0.0
    score += 3.0 if dark_bg else 0.0
    return max(0.0, min(10.0, score))


def make_look(prompt, font, fg, bg):
    return {"prompt": prompt, "font": font, "size": 13,
            "foreground": fg, "background": bg, "palette": "gruvbox"}


def synth_ratings(n=60):
    random.seed(7)
    out = []
    prompts = s.PROMPT_NAMES
    fonts = [LIG_FONT, PLAIN_FONT]
    for _ in range(n):
        p = random.choice(prompts)
        font = random.choice(fonts)
        # pick contrast-valid fg/bg across several dark/light backgrounds
        dark = random.random() < 0.5
        bg = random.choice(DARK_BGS if dark else LIGHT_BGS)
        fg = LIGHT_FG if dark else DARK_FG
        look = make_look(p, font, fg, bg)
        out.append({"look": look, "rating": round(preferred(look))})
    return out


# ----------------------------------------------------------------- (a) signs --
ratings = synth_ratings(60)
model = s.fit_model(ratings)
wmap = dict(zip(s.FEATURE_NAMES, model["mean"]))
# liked two-line: prompt.one_line weight should be NEGATIVE (one_line=0 when liked)
check(wmap["prompt.one_line"] < 0, "weight: one_line < 0 (prefers two-line)")
check(wmap["font.ligatures"] > 0, "weight: ligatures > 0")
check(wmap["bg.luminance"] < 0, "weight: bg.luminance < 0 (prefers dark bg)")


# --------------------------------------------------- (b) rolls trend to liked --
# Persist ratings to state, then roll many learned looks and check the mix.
s.DATA_DIR.mkdir(parents=True, exist_ok=True)
state = {"ratings": ratings, "autoroll": True, "current": s.random_look()}
s.save_all(state)

random.seed(11)
N = 60


def measure(roller):
    tl = lg = dk = 0
    for _ in range(N):
        look = roller()
        f = dict(zip(s.FEATURE_NAMES, s.features(look)))
        tl += f["prompt.one_line"] < 0.5
        lg += f["font.ligatures"] > 0.5
        dk += f["bg.luminance"] < 0.1
    return tl, lg, dk


# baseline: plain random rolls (Stage 1 behaviour)
base_tl, base_lg, base_dk = measure(s.random_look)
# learned rolls
st = s.load_all()
ltl, llg, ldk = measure(lambda: s.learned_look(st))
print(f"      random  rolls: two-line {base_tl}/{N}, ligature {base_lg}/{N}, "
      f"dark-bg {base_dk}/{N}")
print(f"      learned rolls: two-line {ltl}/{N}, ligature {llg}/{N}, "
      f"dark-bg {ldk}/{N}")
# Only 2 of 19 prompts are two-line, so the bar is "clearly beats random", not 60%.
check(ltl > base_tl, "rolls favour two-line vs random baseline")
# Only ~21 of 92 fonts have ligatures (~23% random), so "well above random" is the
# right bar rather than a fixed high percentage.
check(llg >= base_lg * 1.5, "rolls trend ligature font (>=1.5x random)")
check(ldk >= N * 0.6 and ldk > base_dk, "rolls trend dark bg (>=60%)")


# ------------------------------------------------------ (c) learned reports it --
import io  # noqa: E402
import contextlib  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    s.cmd_learned()
report = buf.getvalue()
print("      learned output:")
for line in report.splitlines():
    print("        " + line)
likes_part = report.split("dislike")[0]
dislikes_part = report.split("dislike")[1] if "dislike" in report else ""
# Preferring two-line shows up as DISliking the one-line prompt feature.
check("one-line prompt" in dislikes_part,
      "learned: reports disliking one-line (i.e. prefers two-line)")
check("ligature" in likes_part, "learned: reports liking ligature fonts")
check("bitmap" in dislikes_part, "learned: reports disliking bitmap fonts")


# ------------------------------------------------ (d) cold start stays random --
# Below LEARN_MIN: learning must be off and rolls identical to plain random.
cold = {"ratings": ratings[:s.LEARN_MIN - 1], "autoroll": True}
check(not s._learning_on(cold),
      f"cold start: learning off with {s.LEARN_MIN - 1} ratings")
# At threshold it turns on.
warm = {"ratings": ratings[:s.LEARN_MIN], "autoroll": True}
check(s._learning_on(warm),
      f"learning on at {s.LEARN_MIN} ratings")

# `learned` on cold state should say it's off, not crash.
s.save_all(cold)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    s.cmd_learned()
check("learning off" in buf.getvalue(), "learned: cold start reports 'off'")

# Cold-start roll path uses random_look (no model). Sanity: produces valid look.
s.save_all(cold)
import subprocess  # noqa: E402 (exercise the CLI path end to end)
# Just confirm random_look stays contrast-valid as Stage 1 guaranteed.
ok = True
for _ in range(50):
    lk = s.random_look()
    if s.contrast(lk["foreground"], lk["background"]) < s.MIN_CONTRAST:
        ok = False
check(ok, "cold start: random_look stays contrast-valid")


# -------------------------------------------------------- linalg self-check ---
check(s._linalg_selfcheck(), "linalg self-check (Cholesky solve)")


# ------------------------------------------------------------------- summary --
print()
if FAILS:
    print(f"RESULT: FAIL ({len(FAILS)} failed)")
    for m in FAILS:
        print("  - " + m)
    raise SystemExit(1)
print("RESULT: PASS (all checks)")
