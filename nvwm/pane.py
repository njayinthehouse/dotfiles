#!/usr/bin/env python3
"""
pane — manage neovim panes from the shell.

    pane new {<id>}             create a new pane, auto-named if no id given
    pane rename {<id>} <name>   rename the current pane {or id} to name
    pane ls {<regex>}           list panes, optionally filtered
    pane swap <id1> {<id2>}     swap pane id1 with current {or id2}, incl. GUI
    pane kill {<id>}            kill current pane {or the named one}
    pane goto {<id>}            jump to a pane {or last visited}

Must be run inside a neovim :terminal ($NVIM set). Shares VimTalker with the
WM, so it speaks the same async msgpack-rpc — no pynvim, no second event loop.
Installed as ~/.local/bin/pane by install.sh; imports wm from ~/.local/lib.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.expanduser("~/.local/lib"))

from wm import Handle, NvimRPC, VimTalker


async def resolve(vt: VimTalker, id: str) -> Handle | None:
    """Resolve a pane ID (user name or window-id number) to a window handle."""
    wins = await vt.list_wins()
    # user-assigned names first
    for w in wins:
        if await vt.win_var(w, "pane_id") == id:
            return w
    # then a raw window-id number
    try:
        n = int(id)
    except ValueError:
        return None
    for w in wins:
        if int(w) == n:
            return w
    return None


async def auto_name(vt: VimTalker) -> str:
    """Generate the next pane name: p1, p2, p3, ..."""
    counter = await vt.var("pane_counter")
    n = (counter or 0) + 1
    await vt.set_var("pane_counter", n)
    return f"p{n}"


async def cmd_new(vt: VimTalker, args: list[str]) -> None:
    pane = await vt.create_split()
    if pane is None:
        print("pane new: failed to create split", file=sys.stderr)
        return
    id = args[0] if args else await auto_name(vt)
    await vt.set_win_var(pane.win, "pane_id", id)
    print(id)


async def cmd_ls(vt: VimTalker, args: list[str]) -> None:
    regex = args[0] if args else None
    cur = await vt.current_win()
    for w in await vt.list_wins():
        pid = await vt.win_var(w, "pane_id")
        id = pid if pid else str(w)
        info = await vt.win_buf_info(w)
        if info is None:
            continue
        name, bt = info
        name = name.replace(os.path.expanduser("~"), "~") if name else "[empty]"
        if regex and not (re.search(regex, id) or re.search(regex, name)):
            continue
        mark = ">" if cur is not None and int(w) == int(cur) else " "
        tag = " [terminal]" if bt == "terminal" else ""
        print(f"{mark} {id:<12} {name}{tag}")


async def cmd_kill(vt: VimTalker, args: list[str]) -> None:
    if args:
        w = await resolve(vt, args[0])
        if w is None:
            print(f"pane kill: not found: {args[0]}", file=sys.stderr)
            return
    else:
        w = await vt.current_win()
    if w is None:
        return
    await vt.close_pane(w)
    # WinClosed fires inside neovim -> autocmd notifies the WM; no explicit
    # notify needed here (same for `new` via WinNew).


async def cmd_goto(vt: VimTalker, args: list[str]) -> None:
    if not args:
        # jump to the previous (alternate) window
        await vt.command("wincmd p")
        cur = await vt.current_win()
        if cur is not None:
            info = await vt.win_buf_info(cur)
            if info and info[1] == "terminal":
                await vt.startinsert()
        return

    w = await resolve(vt, args[0])
    if w is None:
        print(f"pane goto: not found: {args[0]}", file=sys.stderr)
        return
    await vt.set_current_win(w)
    info = await vt.win_buf_info(w)
    if info and info[1] == "terminal":
        await vt.startinsert()


async def cmd_rename(vt: VimTalker, args: list[str]) -> None:
    if not args:
        print("usage: pane rename {<id>} <name>", file=sys.stderr)
        return
    # one arg -> rename current pane; two -> rename the named one
    if len(args) >= 2:
        w = await resolve(vt, args[0])
        newname = args[1]
        if w is None:
            print(f"pane rename: not found: {args[0]}", file=sys.stderr)
            return
    else:
        w = await vt.current_win()
        newname = args[0]
    if w is None:
        return
    # refuse a name already taken by a different pane (resolve picks the first
    # match, so duplicates would shadow each other)
    existing = await resolve(vt, newname)
    if existing is not None and int(existing) != int(w):
        print(f"pane rename: name already in use: {newname}", file=sys.stderr)
        return
    await vt.set_win_var(w, "pane_id", newname)
    print(newname)


async def cmd_swap(vt: VimTalker, args: list[str]) -> None:
    if not args:
        print("usage: pane swap <id1> {<id2>}", file=sys.stderr)
        return

    w1 = await resolve(vt, args[0])
    if w1 is None:
        print(f"pane swap: not found: {args[0]}", file=sys.stderr)
        return

    if len(args) >= 2:
        w2 = await resolve(vt, args[1])
        if w2 is None:
            print(f"pane swap: not found: {args[1]}", file=sys.stderr)
            return
    else:
        w2 = await vt.current_win()

    if w2 is None or int(w1) == int(w2):
        print("pane swap: need two distinct panes", file=sys.stderr)
        return

    # swap buffers via the API — window-ids resolved here, no vimscript state
    # (s: variables don't survive across RPC command invocations)
    b1 = await vt.win_buf(w1)
    b2 = await vt.win_buf(w2)
    if b1 is None or b2 is None:
        print("pane swap: could not read window buffers", file=sys.stderr)
        return
    await vt.command(f"call win_execute({int(w1)}, 'buffer {b2}')")
    await vt.command(f"call win_execute({int(w2)}, 'buffer {b1}')")

    # swap pane IDs
    pid1 = await vt.win_var(w1, "pane_id")
    pid2 = await vt.win_var(w2, "pane_id")
    if pid1 is not None:
        await vt.set_win_var(w2, "pane_id", pid1)
    else:
        await vt.del_win_var(w2, "pane_id")
    if pid2 is not None:
        await vt.set_win_var(w1, "pane_id", pid2)
    else:
        await vt.del_win_var(w1, "pane_id")

    # signal the WM to swap GUI placements, then poke it explicitly — a buffer
    # swap changes no window geometry, so no autocmd fires
    await vt.set_var("nvwm_swap_pending", [int(w1), int(w2)])
    await vt.notify()


COMMANDS = {
    "new":    cmd_new,
    "rename": cmd_rename,
    "ls":     cmd_ls,
    "kill":   cmd_kill,
    "goto":   cmd_goto,
    "swap":   cmd_swap,
}


async def amain() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: pane {new|rename|ls|swap|kill|goto} [args...]",
              file=sys.stderr)
        sys.exit(1)
    sock = os.environ.get("NVIM")
    if not sock:
        print("pane: not inside neovim ($NVIM unset)", file=sys.stderr)
        sys.exit(1)
    rpc = NvimRPC(sock)
    try:
        await rpc.connect()
    except OSError as e:
        print(f"pane: failed to connect to neovim: {e}", file=sys.stderr)
        sys.exit(1)
    vt = VimTalker(rpc)
    try:
        await COMMANDS[sys.argv[1]](vt, sys.argv[2:])
    finally:
        await rpc.aclose()


if __name__ == "__main__":
    asyncio.run(amain())
