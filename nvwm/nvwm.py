#!/usr/bin/env python3
"""nvwm entry point. Installed as ~/.local/bin/nvwm by install.sh.

The library module lives in ~/.local/lib (also installed from this repo)."""

import os
import sys

sys.path.insert(0, os.path.expanduser("~/.local/lib"))

from wm import WindowManager

if __name__ == "__main__":
    WindowManager().run()
