#!/usr/bin/env python3
"""Sandbox test for sweettalker's neovim colour apply (nvim_cmds, nvim_apply).

Pure / offline: never touches a live neovim. SWEETTALKER_NO_IPC=1, temp HOME and
SWEETTALKER_DATA, and NVIM explicitly UNSET so nvim_apply no-ops.

Run:  python3 test_nvim.py
"""
import os
import tempfile

# --- isolate everything before importing the module under test ---------------
_TMP = tempfile.mkdtemp(prefix="sweettalker-nvim-test-")
os.environ["SWEETTALKER_NO_IPC"] = "1"
os.environ["HOME"] = _TMP
os.environ["SWEETTALKER_DATA"] = os.path.join(_TMP, "data")
os.environ.pop("NVIM", None)              # ensure nvim_apply is a no-op

import importlib  # noqa: E402
import sweettalker as s  # noqa: E402
importlib.reload(s)

FAILS = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


LOOK = {"prompt": "minimal", "font": "monospace", "size": 13,
        "foreground": "#ebdbb2", "background": "#1d2021", "palette": "gruvbox"}
cmds = s.nvim_cmds(LOOK)
print("      vimscript: " + cmds)

# Normal: fg + bg from the look.
check("hi Normal guifg=#ebdbb2 guibg=#1d2021" in cmds,
      "nvim_cmds: Normal sets look fg/bg")

# Visual + Search use the derived accent (a palette colour, contrast-picked).
accent = s._hl_accent(LOOK)
check(accent.startswith("#") and accent in (s.PALETTES["gruvbox"]["normal"]
                                            + s.PALETTES["gruvbox"]["bright"]),
      "nvim_cmds: accent is a palette colour")
check(f"hi Visual guibg={accent}" in cmds, "nvim_cmds: Visual uses accent")
check(f"hi Search guibg={accent} guifg=#ebdbb2" in cmds,
      "nvim_cmds: Search uses accent + fg")

# All 16 terminal_color_N present, mapping normal[0..7] then bright[0..7].
pal16 = s.PALETTES["gruvbox"]["normal"] + s.PALETTES["gruvbox"]["bright"]
all16 = all(f"let g:terminal_color_{i}='{pal16[i]}'" in cmds for i in range(16))
check(all16, "nvim_cmds: all 16 terminal_color_N map normal[0..7]+bright[0..7]")
check("terminal_color_16" not in cmds, "nvim_cmds: exactly 16, no terminal_color_16")
check(cmds.count(" | ") == 18, "nvim_cmds: 3 hi + 16 lets bar-joined (18 separators)")

# Different palette flows through.
LOOK2 = dict(LOOK, palette="dracula")
cmds2 = s.nvim_cmds(LOOK2)
d16 = s.PALETTES["dracula"]["normal"] + s.PALETTES["dracula"]["bright"]
check(f"let g:terminal_color_5='{d16[5]}'" in cmds2,
      "nvim_cmds: palette swap drives terminal colours")

# nvim_apply with NVIM unset must be a silent no-op (no exception).
try:
    s.nvim_apply(LOOK)
    check(os.environ.get("NVIM") is None, "nvim_apply: no-op when NVIM unset")
except Exception as e:                    # noqa: BLE001
    check(False, f"nvim_apply raised when NVIM unset: {e!r}")

# apply_look (NO_IPC=1) must not raise and must still no-op nvim.
try:
    s.apply_look(LOOK)
    check(True, "apply_look: runs under NO_IPC without touching live nvim")
except Exception as e:                    # noqa: BLE001
    check(False, f"apply_look raised: {e!r}")

print()
if FAILS:
    print(f"RESULT: FAIL ({len(FAILS)} failed)")
    for m in FAILS:
        print("  - " + m)
    raise SystemExit(1)
print("RESULT: PASS (all checks)")
