#!/usr/bin/env python3
"""
sesh — save, restore, and template neovim-driven nvwm sessions.

    sesh layout                 print the current pane layout (read-only)
    sesh save   <name>          snapshot the whole nvim window to ~/.sesh/<name>.sesh
    sesh load   <name>          rebuild a saved session from ~/.sesh/<name>.sesh
    sesh create <name> [args..] instantiate a template from
                                ~/.sesh.templates/<name>.sesh, substituting $1..$N
    sesh new    [name]          background the current session, open a fresh one
    sesh ls                     list open sessions (tabpages)
    sesh switch [name|number]   switch to another open session (or the last one)

A "session" is the entire top-level nvim window: winlayout's row/col tree, each
leaf a pane. Multiple sessions live as neovim tabpages — `new`/`ls`/`switch`
operate on those; the WM hides GUI clients on background tabpages and re-shows
the active tab's. `save`/`load`/`create` act on the current session. A pane is either a :terminal buffer (a command + cwd) or a GUI
client (an app nvwm tiles into the split, marked by the window-local `nvwm_gui`
var the WM sets). sesh is just a serializer/deserializer for that tree:

    layout / save  =  live tree  -> IR  -> text          (capture)
    load   / create=  text -> IR -> live tree            (build)

The text format lives behind emit_text()/parse_text() (currently JSON) so the
.sesh surface language can be swapped in as one isolated change. The GUI-state
capture depth (relaunch-command-only vs per-app hooks) is likewise localized to
_gui_command(). Both are the two open design decisions; everything else here is
independent of them.

Must run inside a neovim :terminal ($NVIM set). Shares wm's async msgpack-rpc —
no pynvim, no second event loop. Installed as ~/.local/bin/sesh by install.sh.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.expanduser("~/.local/lib"))

from wm import Handle, NvimRPC, VimTalker

SESH_DIR = os.path.expanduser("~/.sesh")
TEMPLATE_DIR = os.path.expanduser("~/.sesh.templates")
HOME = os.path.expanduser("~")

# term:// buffer names look like  term://<cwd>//<pid>:<command...>
TERM_RE = re.compile(r"^term://(?P<cwd>.*)//(?P<pid>\d+):(?P<cmd>.*)$")

# CRIU is the one path that preserves *live* state instead of reproducing it:
# a pane whose foreground program is a REPL gets frozen (criu dump) on save and
# revived (criu restore) on load, so in-memory state survives. Everything else
# reproduces (tmux-resurrect style). Spike-proven on this box. criu stays
# root-only by choice (a setcap'd binary is a standing privilege for any caller;
# password-gated sudo is per-invocation and auditable) -> default `sudo criu`,
# which prompts in the terminal (creds cached ~15min). Override with $SESH_CRIU.
CRIU = shlex.split(os.environ.get("SESH_CRIU", "sudo criu"))
REPL_PROGRAMS = {
    "python", "python3", "ipython", "bpython", "ptpython",
    "node", "irb", "pry", "ruby", "iex", "ghci", "julia",
    "R", "scala", "clojure", "clj", "lua", "luajit", "sbcl", "guile", "racket",
}


def _session_dir(name: str) -> str:
    """Per-session sidecar dir for CRIU images (one subdir per frozen pane)."""
    return os.path.join(SESH_DIR, name + ".d")


# ============================================================================
#  Session IR — the in-memory tree everything converts to/from
# ============================================================================

@dataclass
class Leaf:
    kind: str                       # 'term' | 'gui'
    command: str                    # term: shell/cmd line; gui: relaunch cmd line
    name: str | None = None         # pane_id (friendly name), if any
    cwd: str | None = None          # term cwd; None for gui
    wm_class: str | None = None     # gui WM_CLASS instance, for diagnostics
    program: str | None = None      # term: foreground program cmdline, if any
    freeze: str | None = None       # term: CRIU image dir (rel to SESH_DIR) if frozen
    width: int = 0                  # cells
    height: int = 0                 # cells
    # transient (capture-time only, never serialized):
    pid: int = 0                    # the pane's shell pid — the CRIU dump target
    repl: bool = False              # foreground program is a REPL -> freeze it


@dataclass
class Split:
    kind: str                       # 'row' (side by side) | 'col' (stacked)
    children: list = field(default_factory=list)
    width: int = 0
    height: int = 0


Node = object   # Leaf | Split


# ============================================================================
#  Capture — live nvim tree -> IR   (powers `layout` and `save`)
# ============================================================================

class Capture:
    def __init__(self, vt: VimTalker, rpc: NvimRPC) -> None:
        self.vt = vt
        self.rpc = rpc
        self._handles: dict[int, Handle] = {}

    async def run(self) -> Node:
        # winlayout uses bare-int winids; nvim's window APIs want the EXT Handle.
        # Build the int->Handle map once from list_wins().
        for h in await self.vt.list_wins():
            self._handles[int(h)] = h
        layout = await self.rpc.request("nvim_call_function", "winlayout", [])
        return await self._node(layout)

    async def _node(self, spec: list) -> Node:
        kind = spec[0]
        if kind == "leaf":
            return await self._leaf(spec[1])
        children = [await self._node(c) for c in spec[1]]
        w = sum(c.width for c in children) if kind == "row" else (
            children[0].width if children else 0)
        h = sum(c.height for c in children) if kind == "col" else (
            children[0].height if children else 0)
        return Split(kind=kind, children=children, width=w, height=h)

    async def _leaf(self, winid: int) -> Leaf:
        h = self._handles.get(int(winid))
        width = await self.rpc.request("nvim_win_get_width", h) or 0
        height = await self.rpc.request("nvim_win_get_height", h) or 0
        name = await self.vt.win_var(h, "pane_id")
        gui = await self.vt.win_var(h, "nvwm_gui")
        if gui:
            cmd, cls = _gui_command(int(gui))
            return Leaf(kind="gui", command=cmd, name=name, wm_class=cls,
                        width=width, height=height)
        # terminal (or empty) pane: read cwd + command out of the buffer name
        info = await self.vt.win_buf_info(h)
        bufname = info[0] if info else ""
        m = TERM_RE.match(bufname or "")
        if m:
            pid = int(m["pid"])
            program = _foreground_program(pid)   # what's running in the shell
            return Leaf(kind="term", command=m["cmd"], name=name,
                        cwd=_unabbrev(m["cwd"]), program=program,
                        width=width, height=height,
                        pid=pid, repl=_is_repl(program))
        return Leaf(kind="term", command=os.environ.get("SHELL", "/bin/zsh"),
                    name=name, cwd=HOME, width=width, height=height)


def _gui_command(client_id: int) -> tuple[str, str | None]:
    """Best-effort relaunch command for a managed GUI client, via X + /proc.

    THIS is where the open 'how much GUI state to capture' decision lands. v1 is
    relaunch-command-only: read _NET_WM_PID, recover the process's argv. Apps
    with their own session restore (firefox) recover the rest themselves. A
    per-app-hooks variant would extend only this function.
    """
    cls = None
    try:
        from Xlib.display import Display
        from Xlib import Xatom
        dpy = Display()
        win = dpy.create_resource_object("window", client_id)
        try:
            wc = win.get_wm_class()
            cls = wc[0] if wc else None
        except Exception:
            pass
        atom = dpy.intern_atom("_NET_WM_PID")
        prop = win.get_full_property(atom, Xatom.CARDINAL)
        dpy.close()
        if prop and prop.value:
            pid = int(prop.value[0])
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a for a in fh.read().split(b"\0") if a]
            if argv:
                return (" ".join(_shquote(a.decode()) for a in argv), cls)
    except Exception:
        pass
    # couldn't introspect: fall back to the class name as a launch guess
    return (cls or "xterm", cls)


def _foreground_program(pid: int) -> str | None:
    """The command line of the program in the foreground of `pid`'s terminal.

    This is what tmux derives natively (it owns the pty); we reconstruct it the
    same way tmux's Linux osdep does — the controlling tty's foreground process
    group (tpgid) is the running job. If tpgid is the shell itself, the shell is
    at a prompt (no foreground program). Reading /proc/<tpgid>/cmdline recovers
    the full argv (so `nvim foo.txt`, not just `nvim`).
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
        after = data[data.rindex(")") + 1:].split()   # past comm (may hold spaces)
        tpgid = int(after[5])                          # field 8: tty foreground pgrp
    except (OSError, ValueError, IndexError):
        return None
    if tpgid <= 0 or tpgid == pid:
        return None                                    # shell is in the foreground
    try:
        with open(f"/proc/{tpgid}/cmdline", "rb") as fh:
            argv = [a for a in fh.read().split(b"\0") if a]
    except OSError:
        return None
    if not argv:
        return None
    return " ".join(_shquote(a.decode("utf-8", "replace")) for a in argv)


def _is_repl(program: str | None) -> bool:
    if not program:
        return False
    first = program.split()[0]
    return os.path.basename(first) in REPL_PROGRAMS


def _shquote(s: str) -> str:
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def _unabbrev(p: str) -> str:
    return HOME + p[1:] if p == "~" or p.startswith("~/") else p


def _abbrev(p: str | None) -> str | None:
    if not p:
        return p
    return "~" + p[len(HOME):] if p == HOME or p.startswith(HOME + "/") else p


# ============================================================================
#  Build — IR -> live nvim tree   (powers `load` and `create`)
# ============================================================================
#
# Realize a tree starting from one window: split it into a row/col of child
# windows (nvim flattens same-orientation splits, matching winlayout), recurse,
# then fill leaves. Terminal leaves we create directly. GUI leaves we launch and
# let the WM adopt the prepared window instead of making its own split — that's
# the `g:nvwm_adopt_pending` hook added to wm.py's on_map_request.

class Build:
    def __init__(self, vt: VimTalker, rpc: NvimRPC) -> None:
        self.vt = vt
        self.rpc = rpc

    async def run(self, root: Node) -> None:
        start = await self.vt.current_win()
        if start is None:
            print("sesh: no current window to build into", file=sys.stderr)
            return
        leaves: list[tuple[Leaf, Handle]] = []
        await self._realize(root, start, leaves)
        # structure first, then sizes, then leaf contents — GUI launches change
        # focus/geometry, so do them last and sequentially.
        await self._size(root, start)
        for leaf, win in leaves:
            await self._fill(leaf, win)

    async def _realize(self, node: Node, win: Handle,
                       leaves: list[tuple[Leaf, Handle]]) -> None:
        if isinstance(node, Leaf):
            leaves.append((node, win))
            return
        wins = [win]
        split = "belowright vsplit" if node.kind == "row" else "belowright split"
        for _ in range(1, len(node.children)):
            await self.vt.set_current_win(wins[-1])
            await self.vt.command(split)
            cur = await self.vt.current_win()
            if cur is None:
                return
            wins.append(cur)
        for child, w in zip(node.children, wins):
            await self._realize(child, w, leaves)

    async def _size(self, node: Node, win: Handle) -> None:
        # set child extents along the split axis; recurse. Best-effort: nvim
        # clamps to available space and minimum widths.
        if isinstance(node, Leaf):
            return
        wins = await self._axis_windows(win, node)
        for child, w in zip(node.children, wins):
            if w is None:
                continue
            if node.kind == "row" and child.width:
                await self.rpc.request("nvim_win_set_width", w, child.width)
            elif node.kind == "col" and child.height:
                await self.rpc.request("nvim_win_set_height", w, child.height)
        for child, w in zip(node.children, wins):
            if w is not None:
                await self._size(child, w)

    async def _axis_windows(self, win: Handle, node: Split) -> list[Handle | None]:
        # re-derive the child windows from the current winlayout subtree rooted
        # at `win` (structure is built; ids are stable now).
        layout = await self.rpc.request("nvim_call_function", "winlayout", [])
        sub = _find_subtree(layout, int(win))
        handles = {int(h): h for h in await self.vt.list_wins()}
        if not sub or sub[0] != node.kind:
            return [None] * len(node.children)
        out: list[Handle | None] = []
        for child in sub[1]:
            wid = _first_leaf(child)
            out.append(handles.get(wid) if wid is not None else None)
        # pad/truncate to match
        out += [None] * (len(node.children) - len(out))
        return out[:len(node.children)]

    async def _fill(self, leaf: Leaf, win: Handle) -> None:
        await self.vt.set_current_win(win)
        if leaf.name:
            await self.vt.set_win_var(win, "pane_id", leaf.name)
        if leaf.kind == "term":
            if leaf.cwd:
                await self.vt.command(f"lcd {leaf.cwd}")
            if leaf.freeze:
                # revive the frozen REPL: criu restore --shell-job re-attaches the
                # process tree to THIS pane's pty (the terminal it runs in). Live
                # state (REPL variables, &c.) comes back. Quitting it drops to the
                # restored shell, then the criu process exits and closes the pane.
                imgdir = os.path.join(SESH_DIR, leaf.freeze)
                cmd = (f"{' '.join(CRIU)} restore --shell-job "
                       f"-D {_shquote(imgdir)} -o restore.log")
                await self.vt.command(f"terminal {cmd}")
            else:
                await self.vt.command(f"terminal {leaf.command}")
        else:
            # ask the WM to adopt this (current) window for the next mapped
            # client instead of create_split()-ing a fresh one, then launch.
            await self.vt.set_var("nvwm_adopt_pending", 1)
            await self.vt.notify()
            await _spawn(leaf.command)
            # wait until the WM binds a client to this window (nvwm_gui set)
            for _ in range(300):                    # ~3s budget
                if await self.vt.win_var(win, "nvwm_gui"):
                    break
                await asyncio.sleep(0.01)


async def _spawn(command: str) -> None:
    """Launch a GUI app detached from sesh so it outlives this process."""
    await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)


def _find_subtree(spec: list, winid: int):
    """The winlayout subtree whose first leaf is `winid` (identifies the region
    a given window currently roots)."""
    if spec[0] == "leaf":
        return spec if int(spec[1]) == winid else None
    if _first_leaf(spec) == winid:
        return spec
    for child in spec[1]:
        hit = _find_subtree(child, winid)
        if hit is not None:
            return hit
    return None


def _first_leaf(spec: list) -> int | None:
    if spec[0] == "leaf":
        return int(spec[1])
    return _first_leaf(spec[1][0]) if spec[1] else None


# ============================================================================
#  Template substitution   (powers `create <name> <args>`)
# ============================================================================

def substitute(node: Node, args: list[str]) -> None:
    """In-place $1..$N / $@ expansion across leaf commands and cwds."""
    def expand(s: str | None) -> str | None:
        if not s:
            return s
        s = s.replace("$@", " ".join(args))
        for i, a in enumerate(args, 1):
            s = s.replace(f"${i}", a)
        return s

    if isinstance(node, Leaf):
        node.command = expand(node.command) or ""
        node.cwd = expand(node.cwd)
    else:
        for c in node.children:
            substitute(c, args)


# ============================================================================
#  Text seam — IR <-> on-disk .sesh   (JSON today; DSL swaps in here)
# ============================================================================

def emit_text(node: Node) -> str:
    return json.dumps(_to_dict(node), indent=2)


def parse_text(text: str) -> Node:
    return _from_dict(json.loads(text))


def _to_dict(node: Node) -> dict:
    if isinstance(node, Leaf):
        d = {"t": "leaf", "kind": node.kind, "command": node.command,
             "w": node.width, "h": node.height}
        if node.name:
            d["name"] = node.name
        if node.cwd:
            d["cwd"] = _abbrev(node.cwd)
        if node.wm_class:
            d["class"] = node.wm_class
        if node.program:
            d["program"] = node.program
        if node.freeze:
            d["freeze"] = node.freeze
        return d
    return {"t": node.kind, "w": node.width, "h": node.height,
            "children": [_to_dict(c) for c in node.children]}


def _from_dict(d: dict) -> Node:
    if d["t"] == "leaf":
        return Leaf(kind=d["kind"], command=d.get("command", ""),
                    name=d.get("name"), cwd=_unabbrev(d["cwd"]) if d.get("cwd") else None,
                    wm_class=d.get("class"), program=d.get("program"),
                    freeze=d.get("freeze"),
                    width=d.get("w", 0), height=d.get("h", 0))
    return Split(kind=d["t"], width=d.get("w", 0), height=d.get("h", 0),
                 children=[_from_dict(c) for c in d.get("children", [])])


def render_tree(node: Node, indent: int = 0) -> str:
    """Human-readable layout for `sesh layout` (not the on-disk format)."""
    pad = "  " * indent
    if isinstance(node, Leaf):
        nm = (node.name or "-")[:12]
        loc = f"  ({_abbrev(node.cwd)})" if node.cwd else ""
        run = node.program or node.command           # show what's actually running
        tag = "  ❄frozen" if node.freeze else ("  ⟳repl" if node.repl else "")
        return f"{pad}{node.kind:<4} {nm:<12} {run}{loc}{tag}  [{node.width}x{node.height}]"
    lines = [f"{pad}{node.kind}  [{node.width}x{node.height}]"]
    for c in node.children:
        lines.append(render_tree(c, indent + 1))
    return "\n".join(lines)


# ============================================================================
#  CLI
# ============================================================================

def _path(d: str, name: str) -> str:
    return os.path.join(d, name if name.endswith(".sesh") else name + ".sesh")


async def cmd_layout(vt: VimTalker, rpc: NvimRPC, args: list[str]) -> int:
    root = await Capture(vt, rpc).run()
    print(render_tree(root))
    return 0


async def cmd_save(vt: VimTalker, rpc: NvimRPC, args: list[str]) -> int:
    if not args:
        print("usage: sesh save <name>", file=sys.stderr)
        return 1
    name = args[0]
    root = await Capture(vt, rpc).run()
    os.makedirs(SESH_DIR, exist_ok=True)
    _freeze_repls(root, name)                         # CRIU-dump REPL panes
    path = _path(SESH_DIR, name)
    with open(path, "w") as fh:
        fh.write(emit_text(root))
    print(path)
    return 0


def _leaves(node: Node):
    if isinstance(node, Leaf):
        yield node
    else:
        for c in node.children:
            yield from _leaves(c)


def _freeze_repls(root: Node, name: str) -> None:
    """Dump each REPL pane's process tree with CRIU so `load` can revive its
    live state. --leave-running keeps the user's REPL alive after the snapshot.
    A failed dump degrades that pane to reproduction (it loses live state but
    still comes back), never aborting the save."""
    sdir = _session_dir(name)
    for i, leaf in enumerate(_leaves(root)):
        if not (leaf.repl and leaf.pid):
            continue
        imgdir = os.path.join(sdir, str(i))
        os.makedirs(imgdir, exist_ok=True)
        rc = _criu("dump", "--shell-job", "--leave-running",
                   "-t", str(leaf.pid), "-D", imgdir, "-o", "dump.log")
        if rc == 0:
            leaf.freeze = os.path.relpath(imgdir, SESH_DIR)
            print(f"sesh: froze {leaf.name or leaf.pid} ({leaf.program})",
                  file=sys.stderr)
        else:
            print(f"sesh: criu dump failed for {leaf.name or leaf.pid} "
                  f"(rc={rc}); it will reproduce instead. See {imgdir}/dump.log",
                  file=sys.stderr)


def _criu(*argv: str) -> int:
    """Run criu and return its exit code. CRIU defaults to `sudo criu`, so we
    inherit the terminal (no captured pipes) — otherwise sudo can't prompt for
    the password. Diagnostics land in the `-o` logfile, which callers read on
    failure since we can't capture output and still allow the prompt."""
    try:
        return subprocess.run([*CRIU, *argv]).returncode
    except FileNotFoundError:
        return 127


async def cmd_load(vt: VimTalker, rpc: NvimRPC, args: list[str]) -> int:
    if not args:
        print("usage: sesh load <name>", file=sys.stderr)
        return 1
    path = _path(SESH_DIR, args[0])
    if not os.path.exists(path):
        print(f"sesh load: no such session: {path}", file=sys.stderr)
        return 1
    with open(path) as fh:
        root = parse_text(fh.read())
    await Build(vt, rpc).run(root)
    return 0


async def cmd_create(vt: VimTalker, rpc: NvimRPC, args: list[str]) -> int:
    if not args:
        print("usage: sesh create <name> [args...]", file=sys.stderr)
        return 1
    path = _path(TEMPLATE_DIR, args[0])
    if not os.path.exists(path):
        print(f"sesh create: no such template: {path}", file=sys.stderr)
        return 1
    with open(path) as fh:
        root = parse_text(fh.read())
    substitute(root, args[1:])
    await Build(vt, rpc).run(root)
    return 0


# ----------------------------------------------------------------------------
#  Session (tabpage) management — new / ls / switch
# ----------------------------------------------------------------------------

async def _tab_name(rpc: NvimRPC, tab: Handle) -> str | None:
    try:
        return await rpc.request("nvim_tabpage_get_var", tab, "sesh_name")
    except Exception:
        return None


async def _tabs(rpc: NvimRPC) -> list[Handle]:
    return await rpc.request("nvim_list_tabpages") or []


def _nr(tabs: list[Handle], tab: Handle | None) -> int | None:
    """1-based session number of a tabpage handle."""
    if tab is None:
        return None
    for i, t in enumerate(tabs, 1):
        if int(t) == int(tab):
            return i
    return None


async def cmd_new(vt: VimTalker, rpc: NvimRPC, args: list[str]) -> int:
    tabs = await _tabs(rpc)
    cur = await vt.current_tabpage()
    nr = _nr(tabs, cur)
    if nr is not None:
        await vt.set_var("sesh_lasttab", nr)        # so `switch` can come back
    await vt.command("tabnew")                       # creates + enters a blank tab
    tab = await vt.current_tabpage()
    if args and tab is not None:
        await rpc.request("nvim_tabpage_set_var", tab, "sesh_name", args[0])
    await vt.notify()                                # WM hides the backgrounded GUIs
    print(args[0] if args else _nr(await _tabs(rpc), tab))
    return 0


async def cmd_ls(vt: VimTalker, rpc: NvimRPC, args: list[str]) -> int:
    tabs = await _tabs(rpc)
    cur = await vt.current_tabpage()
    for i, t in enumerate(tabs, 1):
        name = await _tab_name(rpc, t)
        wins = await rpc.request("nvim_tabpage_list_wins", t) or []
        mark = ">" if cur is not None and int(t) == int(cur) else " "
        npanes = len(wins)
        print(f"{mark} {i:<3} {(name or '-'):<14} {npanes} pane{'' if npanes == 1 else 's'}")
    return 0


async def cmd_switch(vt: VimTalker, rpc: NvimRPC, args: list[str]) -> int:
    tabs = await _tabs(rpc)
    cur = await vt.current_tabpage()
    target: Handle | None = None
    if not args:
        last = await vt.var("sesh_lasttab")
        if isinstance(last, int) and 1 <= last <= len(tabs):
            target = tabs[last - 1]
    else:
        a = args[0]
        for t in tabs:                               # name takes priority over number
            if await _tab_name(rpc, t) == a:
                target = t
                break
        if target is None and a.isdigit() and 1 <= int(a) <= len(tabs):
            target = tabs[int(a) - 1]
    if target is None:
        print(f"sesh switch: no such session: {args[0] if args else '(no last tab)'}",
              file=sys.stderr)
        return 1
    if cur is not None and int(target) == int(cur):
        return 0                                     # already there
    nr = _nr(tabs, cur)
    if nr is not None:
        await vt.set_var("sesh_lasttab", nr)
    await rpc.request("nvim_set_current_tabpage", target)
    await vt.notify()                                # WM re-shows this session's GUIs
    return 0


COMMANDS = {
    "layout": cmd_layout,
    "save":   cmd_save,
    "load":   cmd_load,
    "create": cmd_create,
    "new":    cmd_new,
    "ls":     cmd_ls,
    "switch": cmd_switch,
}


async def amain() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: sesh {layout|save|load|create|new|ls|switch} [args...]",
              file=sys.stderr)
        return 1
    sock = os.environ.get("NVIM")
    if not sock:
        print("sesh: not inside neovim ($NVIM unset)", file=sys.stderr)
        return 1
    rpc = NvimRPC(sock)
    try:
        await rpc.connect()
    except OSError as e:
        print(f"sesh: failed to connect to neovim: {e}", file=sys.stderr)
        return 1
    vt = VimTalker(rpc)
    try:
        return await COMMANDS[sys.argv[1]](vt, rpc, sys.argv[2:])
    finally:
        await rpc.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
