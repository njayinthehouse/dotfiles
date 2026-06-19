#!/usr/bin/env python3
"""
nvwm — neovim window manager (coroutine edition).

A tiling X11 window manager whose layout is neovim's window layout: every
GUI client lives in a neovim split, and as you resize/move/close splits in
neovim the GUI windows follow. neovim is the single source of truth for
geometry; nvwm just paints pixels onto its cell grid.

Architecture — three classes, one asyncio event loop:

  NvimRPC    — a minimal async msgpack-rpc client over ONE unix socket.
               It multiplexes request/response (queries) AND incoming
               notifications ('nvwm_dirty') on the same connection. This is
               the whole point of the rewrite: the previous version drove a
               synchronous pynvim session inside the asyncio loop, which
               raised "Cannot run the event loop while another loop is
               running" on every call. There is no second event loop here —
               pynvim is gone.

  VimTalker  — neovim API, expressed in cells. Async wrapper over NvimRPC.
               No X11 knowledge. Shared with the `pane` CLI.

  XTalker    — X11 frame management, expressed in pixels. No neovim
               knowledge. Synchronous Xlib, integrated via the loop's fd
               reader (add_reader); X calls are local and sub-millisecond.

  WindowManager — pure glue: maps neovim cells onto X pixels, decides policy.

Concurrency model: coroutines under one TaskGroup, coordinated by a single
dirty flag (asyncio.Event).

  x_consumer  — awaits translated X events off a queue and dispatches them;
                the fd reader callback drains X synchronously into the queue
                (so the X socket never stays readable, no busy-spin)
  nvim_task   — keeps the RPC connection alive: connect (retrying while nvim
                boots), learn our channel id, publish it as _G.nvwm_chan,
                then sleep until the connection drops and reconnect
  resync_task — the only consumer of the dirty flag: debounce 10ms to
                coalesce bursts, then resync every GUI placement

The neovim side lives in nvwm.lua (installed to ~/.config/nvim/plugin/):
autocmds fire 'nvwm_dirty' on WinResized/WinNew/WinClosed/VimResized, which
arrive here as RPC notifications.

Out-of-band refresh paths (independent of the RPC channel):
  - :NvwmRefresh inside neovim
  - SIGUSR1 to the WM process: `pkill -USR1 -f nvwm`

Failure policy: a dead neovim degrades GUI placement to fullscreen, never
silently — nvim_task logs every connect/disconnect to stderr, which
.xinitrc redirects to ~/nvwm.log.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any, Callable, NamedTuple
from Xlib import X, XK, Xatom
from Xlib.display import Display
from Xlib.error import BadAccess, BadWindow
from Xlib.protocol import event as xevent

try:
    import msgpack          # the only RPC dependency (pacman -S python-msgpack)
except ImportError:
    msgpack = None


# --- shared types -----------------------------------------------------------

XWindow = Any       # Xlib.xobject.drawable.Window


class Handle:
    """A neovim Window/Buffer/Tabpage handle.

    neovim's API serializes these as msgpack EXT objects whose payload is the
    msgpack-encoded integer id. We keep that (id, ext-code) pair opaque and
    echo it back unchanged on the next call, so we never need pynvim's type
    machinery. `int(handle)` yields the bare id for places that want a number
    (vimscript win-IDs, buffer numbers); equality against a plain int compares
    by id, which is all the glue and the `pane` CLI rely on.
    """
    __slots__ = ("value", "code")

    def __init__(self, value: int, code: int) -> None:
        self.value = value
        self.code = code

    def __int__(self) -> int: return self.value
    def __index__(self) -> int: return self.value
    def __hash__(self) -> int: return hash(self.value)
    def __repr__(self) -> str: return str(self.value)
    __str__ = __repr__

    def __eq__(self, other: object) -> Any:
        if isinstance(other, Handle):
            return self.value == other.value and self.code == other.code
        if isinstance(other, int):
            return self.value == other
        return NotImplemented


def _ext_hook(code: int, data: bytes) -> Handle:
    return Handle(msgpack.unpackb(data), code)


def _pack_default(obj: Any) -> Any:
    # msgpack calls this only for types it doesn't recognize — i.e. Handle,
    # which is deliberately NOT an int subclass so it lands here and gets
    # re-encoded as the EXT object neovim expects (not a bare integer).
    if isinstance(obj, Handle):
        return msgpack.ExtType(obj.code, msgpack.packb(obj.value))
    raise TypeError(f"cannot serialize {type(obj).__name__}")


class Pane(NamedTuple):
    win:    Handle      # neovim window handle
    row:    int
    col:    int
    width:  int
    height: int


class WMEvent(NamedTuple):
    kind:   str         # 'map_request' | 'mapped' | 'entered' | 'unmapped' | 'destroyed'
    window: XWindow
    arg:    Any = None  # event-specific payload (e.g. the _NET_WM_STATE action)


Rect = tuple[int, int, int, int]   # (x, y, w, h) in pixels


NVIM_CLASS = "nvwm_nvim"
NVIM_SOCK = "/tmp/nvim.sock"
RECONNECT_DELAY_S = 0.5   # retry cadence while nvim's socket is absent/dead
DEBOUNCE_S = 0.01         # coalesce notification bursts before resyncing


# ============================================================================
#  NvimRPC — async msgpack-rpc over one unix socket
# ============================================================================

class NvimError(Exception):
    """A neovim API call returned an error response."""


class NvimRPC:
    """Single-connection msgpack-rpc client.

    Owns one unix-socket connection and a background reader task that demuxes
    the stream into response futures and notification callbacks. Requests from
    every coroutine are multiplexed by message id over the same socket.
    """

    def __init__(self, sock: str = NVIM_SOCK) -> None:
        self.sock = sock
        self.on_notification: Callable[[str, list[Any]], None] | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._unpacker: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._closed: asyncio.Event = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Open the socket and start the reader. Raises OSError if absent."""
        self._reader, self._writer = await asyncio.open_unix_connection(self.sock)
        self._unpacker = msgpack.Unpacker(raw=False, ext_hook=_ext_hook)
        self._closed = asyncio.Event()
        asyncio.create_task(self._read_loop(), name="nvrpc_read")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while data := await self._reader.read(65536):
                self._unpacker.feed(data)
                for msg in self._unpacker:
                    self._handle(msg)
        except (OSError, ConnectionError):
            pass
        finally:
            # EOF or error: fail every in-flight request, mark disconnected.
            self._writer = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("nvim connection closed"))
            self._pending.clear()
            self._closed.set()

    def _handle(self, msg: list[Any]) -> None:
        kind = msg[0]
        if kind == 1:                       # response: [1, id, error, result]
            _, msgid, err, result = msg
            fut = self._pending.pop(msgid, None)
            if fut is not None and not fut.done():
                if err is not None:
                    fut.set_exception(NvimError(err))
                else:
                    fut.set_result(result)
        elif kind == 2:                     # notification: [2, method, params]
            if self.on_notification is not None:
                self.on_notification(msg[1], msg[2])
        elif kind == 0:                     # request: [0, id, method, params]
            # We register no methods; reply with an error so nvim never blocks
            # waiting on us. (In practice nvim never sends us requests.)
            self._send([1, msg[1], "nvwm exposes no methods", None])

    def _send(self, msg: list[Any]) -> None:
        assert self._writer is not None
        self._writer.write(msgpack.packb(msg, default=_pack_default))

    async def request(self, method: str, *params: Any) -> Any:
        """Call a neovim API method and await its result."""
        if not self.connected:
            raise ConnectionError("not connected")
        self._next_id += 1
        msgid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msgid] = fut
        self._send([0, msgid, method, list(params)])
        await self._writer.drain()          # type: ignore[union-attr]
        return await fut

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, ConnectionError):
                pass


# ============================================================================
#  VimTalker — neovim API in cells (async)
# ============================================================================

class VimTalker:
    """Read/write neovim state over an NvimRPC connection. Cell coordinates.

    Every method tolerates a down connection by returning None / [] — the WM
    then degrades a placement to hidden/fullscreen rather than crashing. We do
    NOT log connection-state churn here (that was the old log-spam); nvim_task
    owns and logs the connection lifecycle. Genuine API errors are logged once.
    """

    def __init__(self, rpc: NvimRPC) -> None:
        self.rpc = rpc

    async def _req(self, method: str, *args: Any, quiet: bool = False) -> Any:
        if not self.rpc.connected:
            return None
        try:
            return await self.rpc.request(method, *args)
        except NvimError as e:
            if not quiet:
                print(f"vimtalker: {method}: {e!r}", file=sys.stderr)
            return None
        except (ConnectionError, OSError):
            return None

    # --- notification plumbing ---

    async def publish_chan(self, chan: int) -> bool:
        """Tell neovim which channel to poke; nvwm.lua reads _G.nvwm_chan."""
        if not self.rpc.connected:
            return False
        try:
            await self.rpc.request("nvim_exec_lua", "_G.nvwm_chan = ...", [chan])
            return True
        except Exception as e:
            print(f"vimtalker: publish_chan: {e!r}", file=sys.stderr)
            return False

    async def notify(self) -> None:
        """Poke the WM through the autocmd path. For tools (e.g. `pane swap`)
        whose changes fire no autocmd of their own."""
        await self._req(
            "nvim_exec_lua",
            "if _G.nvwm_chan then vim.rpcnotify(_G.nvwm_chan, 'nvwm_dirty') end",
            [])

    # --- grid / pane queries ---

    async def grid_size(self) -> tuple[int, int] | None:
        """(cols, lines) of the whole editor grid, or None."""
        cols = await self._req("nvim_get_option_value", "columns", {})
        lines = await self._req("nvim_get_option_value", "lines", {})
        if cols is None or lines is None:
            return None
        return (cols, lines)

    async def focused_pane(self) -> Pane | None:
        win = await self._req("nvim_get_current_win")
        return await self._pane_from(win) if win is not None else None

    async def pane_info(self, handle: Handle) -> Pane | None:
        valid = await self._req("nvim_win_is_valid", handle)
        if not valid:
            return None
        return await self._pane_from(handle)

    async def _pane_from(self, handle: Handle) -> Pane | None:
        pos = await self._req("nvim_win_get_position", handle)
        w = await self._req("nvim_win_get_width", handle)
        h = await self._req("nvim_win_get_height", handle)
        if pos is None or w is None or h is None:
            return None
        row, col = pos
        return Pane(win=handle, row=row, col=col, width=w, height=h)

    # --- pane lifecycle ---

    async def create_split(self) -> Pane | None:
        if await self._req("nvim_command", "vnew") is None and not self.rpc.connected:
            return None
        win = await self._req("nvim_get_current_win")
        return await self._pane_from(win) if win is not None else None

    async def close_pane(self, handle: Handle) -> None:
        if await self._req("nvim_win_is_valid", handle):
            await self._req("nvim_win_close", handle, True)   # force=True

    # --- window queries ---

    async def list_wins(self) -> list[Handle]:
        res = await self._req("nvim_list_wins")
        return res if res is not None else []

    async def current_win(self) -> Handle | None:
        return await self._req("nvim_get_current_win")

    async def current_tabpage(self) -> Handle | None:
        return await self._req("nvim_get_current_tabpage")

    async def tabpage_wins(self, tab: Handle) -> list[Handle]:
        """Windows on a tabpage. Used to scope GUI visibility to the active
        sesh session: clients on background tabpages are hidden."""
        res = await self._req("nvim_tabpage_list_wins", tab)
        return res if res is not None else []

    async def set_current_win(self, handle: Handle) -> None:
        await self._req("nvim_set_current_win", handle)

    async def win_buf(self, handle: Handle) -> int | None:
        """Buffer number shown in a window, or None."""
        buf = await self._req("nvim_win_get_buf", handle)
        return int(buf) if buf is not None else None

    async def win_buf_info(self, handle: Handle) -> tuple[str, str] | None:
        """(buffer_name, buftype) for a window."""
        buf = await self._req("nvim_win_get_buf", handle)
        if buf is None:
            return None
        return await self.buf_info(buf)

    async def buf_info(self, buf: int) -> tuple[str, str] | None:
        """(buffer_name, buftype) for a buffer number. Used to describe a
        minimized pane, whose window is gone but whose buffer lives on."""
        name = await self._req("nvim_buf_get_name", buf)
        bt = await self._req("nvim_get_option_value", "buftype", {"buf": buf})
        if name is None or bt is None:
            return None
        return (name, bt)

    # --- window variables (unset is an expected miss -> quiet) ---

    async def win_var(self, handle: Handle, name: str) -> Any:
        return await self._req("nvim_win_get_var", handle, name, quiet=True)

    async def set_win_var(self, handle: Handle, name: str, value: Any) -> None:
        await self._req("nvim_win_set_var", handle, name, value)

    async def del_win_var(self, handle: Handle, name: str) -> None:
        await self._req("nvim_win_del_var", handle, name, quiet=True)

    # --- global variables ---

    async def var(self, name: str) -> Any:
        return await self._req("nvim_get_var", name, quiet=True)

    async def set_var(self, name: str, value: Any) -> None:
        await self._req("nvim_set_var", name, value)

    # --- commands ---

    async def command(self, cmd: str) -> None:
        await self._req("nvim_command", cmd)

    async def startinsert(self) -> None:
        await self.command("startinsert")

    async def input(self, keys: str) -> None:
        """Feed key input to neovim (e.g. '<C-\\><C-n>' to force normal mode).
        Unlike nvim_command, this respects neovim's mode machinery, so it can
        leave terminal-insert mode where :stopinsert cannot."""
        await self._req("nvim_input", keys)


# ============================================================================
#  XTalker — X11 frame management in pixels (synchronous Xlib)
# ============================================================================

class XTalker:
    """Manage X11 frames at pixel coordinates. No neovim knowledge."""

    def __init__(self) -> None:
        self.dpy: Display = Display()
        self.screen = self.dpy.screen()
        self.root: XWindow = self.screen.root
        self._frames: dict[int, XWindow] = {}     # client id -> frame
        self._clients: dict[int, XWindow] = {}    # frame id  -> client
        self._ignore_unmaps: int = 0
        self._focused: int | None = None
        self._atom_wm_type = self.dpy.intern_atom('_NET_WM_WINDOW_TYPE')
        self._atom_type_normal = self.dpy.intern_atom('_NET_WM_WINDOW_TYPE_NORMAL')
        # EWMH fullscreen — apps (notably SDL/Steam games) toggle this via a
        # _NET_WM_STATE ClientMessage to root, or preset it before mapping.
        self._atom_net_supported = self.dpy.intern_atom('_NET_SUPPORTED')
        self._atom_net_wm_state = self.dpy.intern_atom('_NET_WM_STATE')
        self._atom_net_wm_state_fs = self.dpy.intern_atom('_NET_WM_STATE_FULLSCREEN')
        self._atom_net_wm_check = self.dpy.intern_atom('_NET_SUPPORTING_WM_CHECK')
        self._atom_net_wm_name = self.dpy.intern_atom('_NET_WM_NAME')
        self._atom_utf8 = self.dpy.intern_atom('UTF8_STRING')
        # ICCCM WM_STATE (4.1.3.1). Reparenting WMs MUST set this on the managed
        # client window: toolkits walk the tree from the root and identify the
        # real client toplevel under the pointer by its WM_STATE. Without it,
        # GTK's drag target-finding can't resolve a drop window, so Firefox tab
        # drag/merge between windows fails (the tab snaps back) even though the
        # client has XdndAware. Same property that xdotool/xwininfo rely on.
        self._atom_wm_state = self.dpy.intern_atom('WM_STATE')
        self._wm_check: XWindow | None = None
        self._esc_kc = self.dpy.keysym_to_keycode(XK.XK_Escape)

    def claim_wm(self) -> None:
        """Grab SubstructureRedirect on root, or exit if a WM already has it."""
        failed: bool = False

        def on_error(err: Any, *a: Any) -> int:
            nonlocal failed
            if isinstance(err, BadAccess):
                failed = True
            return 0

        self.dpy.set_error_handler(on_error)
        self.root.change_attributes(
            event_mask=(X.SubstructureRedirectMask | X.SubstructureNotifyMask))
        self.dpy.sync()
        if failed:
            sys.exit("another WM is already running on this display")

        # From here on, swallow async X errors (BadWindow races are routine
        # for a reparenting WM) but never silently — keep the trail in the log.
        def log_error(err: Any, *a: Any) -> int:
            print(f"x11: {err.__class__.__name__}: {err}", file=sys.stderr)
            return 0

        self.dpy.set_error_handler(log_error)

    def grab_keys(self) -> None:
        """Reserve the global modal chord: Ctrl+Escape -> normal mode.

        A passive grab on root fires regardless of which window holds the
        keyboard — including a focused GUI client, which otherwise swallows
        every key and would leave the user stuck with no way back to neovim.
        We grab the Lock/NumLock-modified variants too so the chord works with
        CapsLock or NumLock on."""
        for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
            self.root.grab_key(self._esc_kc, X.ControlMask | extra, False,
                               X.GrabModeAsync, X.GrabModeAsync)
        self.dpy.sync()

    def advertise_ewmh(self) -> None:
        """Announce ourselves as an EWMH-aware WM that supports fullscreen.

        Toolkits (SDL, GTK, SFML) only take the managed `_NET_WM_STATE_FULLSCREEN`
        path if they can see (a) a `_NET_SUPPORTING_WM_CHECK` window proving a
        conformant WM is present and (b) fullscreen listed in `_NET_SUPPORTED`.
        Without this they fall back to override-redirect grabs that bypass us
        entirely. The check window is an off-screen InputOnly child that just has
        to exist and carry our name.
        """
        self._wm_check = self.root.create_window(
            -1, -1, 1, 1, 0, X.CopyFromParent, X.InputOnly, X.CopyFromParent)
        for win in (self.root, self._wm_check):
            win.change_property(self._atom_net_wm_check, Xatom.WINDOW, 32,
                                [self._wm_check.id])
        self._wm_check.change_property(self._atom_net_wm_name, self._atom_utf8,
                                       8, b"nvwm")
        self.root.change_property(
            self._atom_net_supported, Xatom.ATOM, 32,
            [self._atom_net_wm_state, self._atom_net_wm_state_fs,
             self._atom_net_wm_check])
        self.dpy.flush()

    def screen_size(self) -> tuple[int, int]:
        return (self.screen.width_in_pixels, self.screen.height_in_pixels)

    def wm_class(self, window: XWindow) -> tuple[str, str] | None:
        try:
            return window.get_wm_class()
        except BadWindow:
            return None

    def is_normal_window(self, window: XWindow) -> bool:
        """Regular top-level app window (not a dialog/utility/splash)?"""
        try:
            if window.get_wm_transient_for() is not None:
                return False
        except BadWindow:
            return False
        try:
            prop = window.get_full_property(self._atom_wm_type, X.AnyPropertyType)
            if prop is not None:
                return self._atom_type_normal in prop.value
            return True   # no type set -> EWMH default is NORMAL
        except BadWindow:
            return False

    def _client_window(self, client_id: int) -> XWindow | None:
        frm = self._frames.get(client_id)
        return self._clients.get(frm.id) if frm is not None else None

    def wants_fullscreen(self, window: XWindow) -> bool:
        """Does this window already declare `_NET_WM_STATE_FULLSCREEN`?
        Checked at map time so a game that presets the hint comes up full."""
        try:
            prop = window.get_full_property(self._atom_net_wm_state, Xatom.ATOM)
        except BadWindow:
            return False
        return prop is not None and self._atom_net_wm_state_fs in prop.value

    def set_net_fullscreen(self, client_id: int, on: bool) -> None:
        """Write `_NET_WM_STATE_FULLSCREEN` into the client's state, so an app
        that asked to (un)fullscreen sees the WM confirm it."""
        client = self._client_window(client_id)
        if client is None:
            return
        try:
            prop = client.get_full_property(self._atom_net_wm_state, Xatom.ATOM)
            atoms = [a for a in (prop.value if prop is not None else [])
                     if a != self._atom_net_wm_state_fs]
            if on:
                atoms.append(self._atom_net_wm_state_fs)
            client.change_property(self._atom_net_wm_state, Xatom.ATOM, 32, atoms)
        except BadWindow:
            pass

    def _set_wm_state(self, client: XWindow, state: int) -> None:
        """Set ICCCM WM_STATE on the client (NormalState=1, WithdrawnState=0).
        Type is the WM_STATE atom itself; value is [state, icon_window]."""
        try:
            client.change_property(self._atom_wm_state, self._atom_wm_state, 32,
                                   [state, X.NONE])
        except BadWindow:
            pass

    def frame(self, client: XWindow, x: int, y: int, w: int, h: int) -> None:
        """Create a frame at (x, y, w, h) and reparent the client into it."""
        if client.id in self._frames:
            return
        frm: XWindow = self.root.create_window(
            x, y, max(1, w), max(1, h), 0,
            self.screen.root_depth, X.InputOutput, X.CopyFromParent,
            background_pixel=self.screen.black_pixel,
            event_mask=(X.SubstructureRedirectMask | X.SubstructureNotifyMask))
        self._ignore_unmaps += 1
        try:
            client.reparent(frm, 0, 0)
        except BadWindow:
            frm.destroy()
            return
        client.configure(width=max(1, w), height=max(1, h))
        # Click-to-focus: a SYNCHRONOUS button grab freezes the pointer on press
        # so we can switch focus/mode first, then ReplayPointer (see _translate)
        # delivers the same click to the app — nothing is swallowed. This is the
        # only path that changes focus now; hovering deliberately does not.
        frm.grab_button(1, X.AnyModifier, False, X.ButtonPressMask,
                        X.GrabModeSync, X.GrabModeAsync, X.NONE, X.NONE)
        frm.map()
        client.map()
        self._set_wm_state(client, 1)  # NormalState — ICCCM, enables DND target-finding
        self._frames[client.id] = frm
        self._clients[frm.id] = client
        self._notify_configure(client, x, y, max(1, w), max(1, h))

    def unframe(self, client_id: int) -> None:
        frm = self._frames.pop(client_id, None)
        if frm is None:
            return
        client = self._clients.pop(frm.id, None)
        if self._focused == client_id:
            self._focused = None
        if client is not None:
            self._set_wm_state(client, 0)  # WithdrawnState — ICCCM, no longer managed
        try:
            frm.destroy()
        except BadWindow:
            pass

    def move_resize(self, client_id: int, x: int, y: int, w: int, h: int) -> None:
        frm = self._frames.get(client_id)
        if frm is None:
            return
        w, h = max(1, w), max(1, h)
        try:
            frm.configure(x=x, y=y, width=w, height=h)
            client = self._clients.get(frm.id)
            if client:
                client.configure(width=w, height=h)
                self._notify_configure(client, x, y, w, h)
        except BadWindow:
            pass

    def _notify_configure(self, client: XWindow,
                          x: int, y: int, w: int, h: int) -> None:
        """Synthetic ConfigureNotify with ROOT coordinates (ICCCM 4.1.5).

        Reparented clients otherwise never learn where they sit on the root
        window, which breaks anything doing global hit-testing — notably
        Firefox tab drag/merge between windows. Frame border is 0 and clients
        sit at (0,0) in their frame, so client root coords equal frame coords.
        """
        ev = xevent.ConfigureNotify(
            event=client, window=client, x=x, y=y, width=w, height=h,
            border_width=0, above_sibling=X.NONE, override=0)
        try:
            client.send_event(ev, event_mask=X.StructureNotifyMask)
        except BadWindow:
            pass

    def focus(self, client_id: int) -> bool:
        frm = self._frames.get(client_id)
        if frm is None:
            return False
        client = self._clients.get(frm.id)
        if client is None:
            return False
        try:
            if client.get_attributes().map_state != X.IsViewable:
                return False
            client.set_input_focus(X.RevertToParent, X.CurrentTime)
            self._focused = client_id
            self.dpy.sync()
            return True
        except BadWindow:
            return False

    @property
    def focused(self) -> int | None:
        return self._focused

    def set_stacking(self, client_id: int, mode: int) -> None:
        """mode: X.Above or X.Below."""
        frm = self._frames.get(client_id)
        if frm is None:
            return
        try:
            frm.configure(stack_mode=mode)
        except BadWindow:
            pass

    def hide(self, client_id: int) -> None:
        frm = self._frames.get(client_id)
        if frm is None:
            return
        try:
            frm.unmap()
        except BadWindow:
            pass

    def show(self, client_id: int) -> None:
        frm = self._frames.get(client_id)
        if frm is None:
            return
        try:
            frm.map()
        except BadWindow:
            pass

    def is_managed(self, client_id: int) -> bool:
        return client_id in self._frames

    def fileno(self) -> int:
        return self.dpy.fileno()

    def flush(self) -> None:
        """Push buffered requests to the server. Must run before sleeping, or
        queued configures sit in the output buffer."""
        try:
            self.dpy.flush()
        except Exception:
            pass

    def has_pending(self) -> bool:
        """Events sitting in python-xlib's INTERNAL buffer. Fd readability is
        not the whole story: any round-trip (get_geometry etc.) can slurp
        events into the library buffer while the fd goes quiet, so callers
        must drain until this is False, not until the fd is."""
        try:
            return self.dpy.pending_events() > 0
        except Exception:
            return False

    def drain(self) -> list[WMEvent]:
        """Translate every currently-pending X event. Non-blocking."""
        events: list[WMEvent] = []
        for _ in range(self.dpy.pending_events()):
            ev = self.dpy.next_event()
            result = self._translate(ev)
            if result is not None:
                events.append(result)
        return events

    def _translate(self, ev: Any) -> WMEvent | None:
        if ev.type == X.MapRequest:
            return WMEvent("map_request", ev.window)
        elif ev.type == X.MapNotify:
            if ev.window.id in self._frames:
                return WMEvent("mapped", ev.window)
        elif ev.type == X.KeyPress:
            if ev.detail == self._esc_kc and ev.state & X.ControlMask:
                return WMEvent("to_normal", self.root)
        elif ev.type == X.ButtonPress:
            # Pointer is frozen by the sync grab; release it back to the app
            # immediately (focus/mode switch happens in the async handler), so
            # the click still lands where the user aimed.
            client = self._clients.get(ev.window.id)
            try:
                self.dpy.allow_events(X.ReplayPointer, ev.time)
            except BadWindow:
                pass
            if client is not None:
                return WMEvent("clicked", client)
        elif ev.type == X.ClientMessage:
            # EWMH _NET_WM_STATE: data = [action, prop1, prop2, source, 0].
            # action 0=remove 1=add 2=toggle. We act only on the fullscreen bit.
            if ev.client_type == self._atom_net_wm_state:
                fmt, vals = ev.data
                if fmt == 32 and self._atom_net_wm_state_fs in (vals[1], vals[2]):
                    if ev.window.id in self._frames:
                        return WMEvent("net_fullscreen", ev.window, vals[0])
        elif ev.type == X.ConfigureRequest:
            self._handle_configure(ev)
            return None
        elif ev.type == X.UnmapNotify:
            if self._ignore_unmaps > 0:
                self._ignore_unmaps -= 1
                return None
            if ev.window.id in self._frames:
                self.unframe(ev.window.id)
                return WMEvent("unmapped", ev.window)
        elif ev.type == X.DestroyNotify:
            if ev.window.id in self._frames:
                self.unframe(ev.window.id)
                return WMEvent("destroyed", ev.window)
        return None

    def _handle_configure(self, ev: Any) -> None:
        """Force framed clients to fill their frame; honor unframed ones."""
        if ev.window.id in self._frames:
            frm = self._frames[ev.window.id]
            try:
                g = frm.get_geometry()
                ev.window.configure(x=0, y=0, width=g.width, height=g.height)
                self._notify_configure(ev.window, g.x, g.y, g.width, g.height)
            except BadWindow:
                pass
        else:
            ev.window.configure(x=ev.x, y=ev.y, width=ev.width,
                                height=ev.height, border_width=ev.border_width)


# ============================================================================
#  WindowManager — glue
# ============================================================================

class WindowManager:

    def __init__(self) -> None:
        self.rpc = NvimRPC(NVIM_SOCK)
        self.vim = VimTalker(self.rpc)
        self.x = XTalker()
        self.x.claim_wm()
        self.x.grab_keys()                        # reserve Ctrl+Esc globally
        self.x.advertise_ewmh()                   # announce EWMH fullscreen support
        self.mode: str = "insert"                 # 'insert' (content has kbd) | 'normal'
        self.fullscreen: int | None = None        # client id flagged fullscreen (≤1)
        self._fullscreen_app = False              # True if app/EWMH-driven (vs `pane full`)
        sw, sh = self.x.screen_size()
        self.sw, self.sh = sw, sh
        self.nvim_host_id: int | None = None
        self.host_geom: Rect | None = None
        self.placements: dict[int, Handle] = {}   # X client id -> vim window
        self._dirty: asyncio.Event | None = None  # created inside the loop
        self._xq: asyncio.Queue[WMEvent] | None = None
        self._dispatch: dict[str, Callable[[XWindow], Any]] = {
            "map_request": self.on_map_request,
            "mapped":      self.on_mapped,
            "clicked":     self.on_clicked,
            "to_normal":   self.on_to_normal,
            "unmapped":    self.on_unmapped,
            "destroyed":   self.on_destroyed,
        }
        # event kinds that change layout and therefore warrant a resync
        self._dirtying: frozenset[str] = frozenset(
            {"map_request", "unmapped", "destroyed", "net_fullscreen"})

    # --- cell <-> pixel ---

    async def pane_to_pixels(self, pane: Pane) -> Rect | None:
        """Convert a Pane (cells) to (x, y, w, h) in pixels.

        TODO: fine-tune alignment — the GUI slightly covers the status bar.
        Likely need to account for terminal padding offset.
        """
        if self.host_geom is None:
            return None
        grid = await self.vim.grid_size()
        if grid is None:
            return None
        cols, lines = grid
        hx, hy, hw, hh = self.host_geom
        cw = hw // cols
        ch = hh // lines
        return (hx + pane.col * cw, hy + pane.row * ch,
                max(1, pane.width * cw), max(1, pane.height * ch))

    # --- event handlers (async: may talk to neovim) ---

    async def on_map_request(self, client: XWindow) -> None:
        if self.x.is_managed(client.id):
            return
        cls = self.x.wm_class(client)
        is_host = cls is not None and NVIM_CLASS in cls
        is_normal = self.x.is_normal_window(client)
        print(f"map_request: id={client.id:#x} class={cls} normal={is_normal}",
              file=sys.stderr)

        pane: Pane | None = None
        if is_host:
            self.nvim_host_id = client.id
            x, y, w, h = 0, 0, self.sw, self.sh
        else:
            # every normal GUI gets its own pane; dialogs/utilities overlay
            # the focused pane without splitting.
            if is_normal:
                # `sesh` builds the target window tree up front, then launches
                # each GUI leaf with g:nvwm_adopt_pending set so we bind the
                # client to its prepared (current) window instead of splitting
                # off a new one — same pending-var protocol as swap/full/min.
                if await self.vim.var("nvwm_adopt_pending"):
                    await self.vim.set_var("nvwm_adopt_pending", 0)
                    pane = await self.vim.focused_pane()
                else:
                    pane = await self.vim.create_split()
            else:
                pane = await self.vim.focused_pane()
            rect = await self.pane_to_pixels(pane) if pane else None
            if rect is not None:
                x, y, w, h = rect
            else:
                # Diagnose, don't just degrade: pane=None means a dead
                # connection, rect=None means no host yet.
                print(f"nvwm: fullscreen fallback for {client.id:#x} "
                      f"(pane={pane}, host_geom={self.host_geom})",
                      file=sys.stderr)
                x, y, w, h = 0, 0, self.sw, self.sh

        self.x.frame(client, x, y, w, h)

        if is_host:
            self.host_geom = (x, y, w, h)
            self.x.set_stacking(client.id, X.Below)
        elif is_normal and pane is not None:
            self.placements[client.id] = pane.win
            # mark the placeholder window so neovim's normal-mode `i` knows this
            # pane hands the keyboard to a GUI client rather than entering insert
            await self.vim.set_win_var(pane.win, "nvwm_gui", client.id)
            self.x.set_stacking(client.id, X.Above)
            # a game that presets _NET_WM_STATE_FULLSCREEN comes up full
            if self.x.wants_fullscreen(client):
                await self._set_fullscreen(client.id, app=True)
        else:
            self.x.set_stacking(client.id, X.Above)

    async def on_mapped(self, client: XWindow) -> None:
        # A freshly mapped GUI grabs the keyboard and becomes the current pane,
        # i.e. launching an app drops you straight into it (insert/GUI mode).
        self.x.focus(client.id)
        if client.id != self.nvim_host_id:
            win = self.placements.get(client.id)
            if win is not None:
                await self.vim.set_current_win(win)
            await self._set_mode("insert")
        self.poke()                               # re-stack any fullscreen client

    async def on_clicked(self, client: XWindow) -> None:
        """Click-to-focus: enter the clicked pane's content (insert/GUI mode).

        The click itself was already replayed to the app (see XTalker); here we
        just move the keyboard. For a GUI pane we also make its neovim window
        current, so a later Ctrl+Esc -> `i` round-trips to the same pane. For
        the neovim host, neovim's own mouse handling picks the pane and, for a
        terminal, drops into terminal mode (see nvwm.lua)."""
        self.x.focus(client.id)
        if client.id != self.nvim_host_id:
            win = self.placements.get(client.id)
            if win is not None:
                await self.vim.set_current_win(win)
        await self._set_mode("insert")
        self.poke()                               # re-stack any fullscreen client

    async def on_to_normal(self, _window: XWindow) -> None:
        """Ctrl+Esc from anywhere -> normal mode: pull the keyboard back to the
        neovim host and force it out of insert/terminal mode."""
        # A manual `pane full` is restored to its tiled geometry on Ctrl+Esc;
        # app/EWMH (game) fullscreens stay full and merely drop below the host,
        # since clearing them would force a resize the app fights (flicker).
        if self.fullscreen is not None and not self._fullscreen_app:
            await self._set_fullscreen(None)
        if self.nvim_host_id is not None:
            self.x.focus(self.nvim_host_id)
        await self.vim.input(r"<C-\><C-n>")
        await self._set_mode("normal")
        self.poke()       # drop any fullscreen client below the host so neovim shows

    async def _enter_gui(self) -> None:
        """Hand the keyboard to the GUI client of neovim's current pane.
        Triggered by the neovim-side `i` mapping (nvwm_enter_gui)."""
        win = await self.vim.current_win()
        if win is None:
            return
        for cid, w in self.placements.items():
            if int(w) == int(win) and self.x.is_managed(cid):
                self.x.focus(cid)
                self.x.set_stacking(cid, X.Above)
                await self._set_mode("insert")
                self.poke()                       # re-stack if this is fullscreen
                return

    async def on_net_fullscreen(self, client: XWindow, action: int) -> None:
        """Honor an app's _NET_WM_STATE_FULLSCREEN request (add/remove/toggle)."""
        cid = client.id
        if not self.x.is_managed(cid):
            return
        on = {0: False, 1: True}.get(action, self.fullscreen != cid)  # 2=toggle
        await self._set_fullscreen(cid if on else None, app=True)

    async def _set_fullscreen(self, cid: int | None, app: bool = False) -> None:
        """Flag (or clear) the single fullscreen client. resync() does the
        actual sizing/stacking; here we just record it, write the EWMH state
        back so the app gets confirmation, and pull focus onto a new one.
        `app` marks an app/EWMH-driven request so Ctrl+Esc knows not to
        un-fullscreen it (see on_to_normal)."""
        prev = self.fullscreen
        if prev is not None and prev != cid:
            self.x.set_net_fullscreen(prev, False)
        self.fullscreen = cid
        self._fullscreen_app = app if cid is not None else False
        if cid is not None:
            self.x.set_net_fullscreen(cid, True)
            self.x.focus(cid)
            await self._set_mode("insert")
        elif prev is not None:
            self.x.set_net_fullscreen(prev, False)
        self.poke()

    async def _set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        await self.vim.set_var("nvwm_mode", mode)   # for the statusline chip

    async def on_unmapped(self, client: XWindow) -> None:
        await self._forget(client.id)

    async def on_destroyed(self, client: XWindow) -> None:
        await self._forget(client.id)

    async def _forget(self, client_id: int) -> None:
        if self.fullscreen == client_id:
            self.fullscreen = None
        vim_win = self.placements.pop(client_id, None)
        if vim_win is not None:
            await self.vim.close_pane(vim_win)
        else:
            # no placement: might be a minimized client the user just closed
            await self._prune_minimized(client_id)
        if client_id == self.nvim_host_id:
            self.nvim_host_id = None
            self.host_geom = None

    # --- live sync ---

    async def _check_swap_signal(self) -> None:
        """Honor a swap requested by `pane swap` via g:nvwm_swap_pending.

        The pane tool swaps two windows' buffers (which moves no geometry, so
        no autocmd fires) then sets g:nvwm_swap_pending = [winid1, winid2] and
        notifies us. We mirror that by swapping which X client maps to which
        neovim window, so a GUI client follows its buffer to the new slot.
        """
        swap = await self.vim.var("nvwm_swap_pending")
        if not isinstance(swap, list) or len(swap) != 2:
            return
        await self.vim.set_var("nvwm_swap_pending", 0)
        wh1, wh2 = int(swap[0]), int(swap[1])
        # resolve ids to live Window handles so post-swap pane_info works
        by_int = {int(w): w for w in await self.vim.list_wins()}
        h1, h2 = by_int.get(wh1), by_int.get(wh2)
        cid1 = cid2 = None
        for cid, wh in self.placements.items():
            if int(wh) == wh1:
                cid1 = cid
            elif int(wh) == wh2:
                cid2 = cid
        if cid1 is not None and cid2 is not None:
            self.placements[cid1], self.placements[cid2] = (
                self.placements[cid2], self.placements[cid1])
        elif cid1 is not None and h2 is not None:
            self.placements[cid1] = h2
        elif cid2 is not None and h1 is not None:
            self.placements[cid2] = h1

    async def _check_fullscreen_signal(self) -> None:
        """Honor `pane full`, which sets g:nvwm_fullscreen_pending = <winid> and
        notifies us. We map that neovim window to its GUI client and toggle the
        fullscreen flag on it (clearing it if that client is already full)."""
        req = await self.vim.var("nvwm_fullscreen_pending")
        if not req:
            return
        await self.vim.set_var("nvwm_fullscreen_pending", 0)
        winid = int(req)
        cid = next((c for c, wh in self.placements.items()
                    if int(wh) == winid), None)
        if cid is None:
            return                    # a neovim-only pane has no client to blow up
        await self._set_fullscreen(None if self.fullscreen == cid else cid)

    async def _check_minimize_signal(self) -> None:
        """Honor `pane min`, which sets g:nvwm_minimize_pending = <winid> and
        notifies us. We stash the pane's GUI client (hidden, dropped from the
        placement map so resync leaves it alone), record it in the shared
        g:nvwm_minimized registry keyed by pane name, then close the split.
        The neovim buffer survives hidden, so a pure terminal/editor pane is
        restorable too — its client id is just 0.
        """
        req = await self.vim.var("nvwm_minimize_pending")
        if not req:
            return
        await self.vim.set_var("nvwm_minimize_pending", 0)
        winid = int(req)
        win = next((w for w in await self.vim.list_wins()
                    if int(w) == winid), None)
        if win is None:
            return
        name = await self.vim.win_var(win, "pane_id") or str(winid)
        buf = await self.vim.win_buf(win)
        cid = next((c for c, wh in self.placements.items()
                    if int(wh) == winid), 0)
        if cid:
            self.placements.pop(cid, None)
            if self.fullscreen == cid:
                self.fullscreen = None
            self.x.hide(cid)
        mini = await self.vim.var("nvwm_minimized")
        if not isinstance(mini, dict):
            mini = {}
        mini[name] = [buf if buf is not None else 0, cid]
        await self.vim.set_var("nvwm_minimized", mini)
        await self.vim.close_pane(win)            # buffer lives on, hidden

    async def _check_restore_signal(self) -> None:
        """Honor `pane goto <name>` on a minimized pane: it sets
        g:nvwm_restore_pending = <name> and notifies us. We open a fresh split,
        re-show the saved buffer, and re-bind the GUI client (if any) so it
        follows the new window again."""
        name = await self.vim.var("nvwm_restore_pending")
        if not name or not isinstance(name, str):
            return
        await self.vim.set_var("nvwm_restore_pending", "")
        mini = await self.vim.var("nvwm_minimized")
        if not isinstance(mini, dict) or name not in mini:
            return
        entry = mini[name]
        buf, cid = int(entry[0]), int(entry[1])
        pane = await self.vim.create_split()
        if pane is None:
            return
        win = pane.win
        empty = await self.vim.win_buf(win)       # throwaway buffer from :vnew
        if buf:
            await self.vim.command(
                f"call win_execute({int(win)}, 'buffer {buf}')")
            if empty is not None and int(empty) != buf:
                await self.vim.command(f"silent! bwipeout {int(empty)}")
        await self.vim.set_win_var(win, "pane_id", name)
        if cid:
            self.placements[cid] = win
            await self.vim.set_win_var(win, "nvwm_gui", cid)
            self.x.show(cid)
            self.x.focus(cid)
        del mini[name]
        await self.vim.set_var("nvwm_minimized", mini)
        await self.vim.set_current_win(win)
        self.poke()

    async def _prune_minimized(self, cid: int) -> None:
        """Drop any registry entry for a minimized client that has died (the
        user closed the app while it was minimized), so it stops haunting
        `pane ls` and `pane goto`."""
        mini = await self.vim.var("nvwm_minimized")
        if not isinstance(mini, dict):
            return
        stale = [n for n, e in mini.items() if e and int(e[1]) == cid]
        if not stale:
            return
        for n in stale:
            del mini[n]
        await self.vim.set_var("nvwm_minimized", mini)

    async def resync(self) -> None:
        if self.host_geom is None:
            return
        await self._check_swap_signal()
        await self._check_fullscreen_signal()
        await self._check_minimize_signal()
        await self._check_restore_signal()
        fs = self.fullscreen
        if fs is not None and not self.x.is_managed(fs):
            fs = self.fullscreen = None
        # tab-scoped visibility: each sesh session is a tabpage, and neovim only
        # renders the current tab's windows. Clients tiled into a background
        # tab's windows must be hidden until that session is switched to. One
        # query gives the active tab's window set; membership is checked in-mem.
        cur_tab = await self.vim.current_tabpage()
        cur_wins = ({int(w) for w in await self.vim.tabpage_wins(cur_tab)}
                    if cur_tab is not None else None)
        for cid, vim_win in list(self.placements.items()):
            if not self.x.is_managed(cid):
                self.placements.pop(cid, None)
                continue
            if cur_wins is not None and int(vim_win) not in cur_wins:
                self.x.hide(cid)                  # on another sesh session
                continue
            if cid == fs:
                # The fullscreen client is always screen-sized; stacking tracks
                # focus — raised above everything while focused, dropped below
                # the neovim host (so the tiled workspace shows) once you leave
                # it via Ctrl+Esc. No resize on focus change → no game flicker.
                self.x.show(cid)
                self.x.move_resize(cid, 0, 0, self.sw, self.sh)
                self.x.set_stacking(
                    cid, X.Above if self.x.focused == cid else X.Below)
                continue
            pane = await self.vim.pane_info(vim_win)
            if pane is None:
                self.x.hide(cid)
                continue
            rect = await self.pane_to_pixels(pane)
            if rect is None:
                continue
            x, y, w, h = rect
            self.x.show(cid)
            self.x.move_resize(cid, x, y, w, h)
            self.x.set_stacking(cid, X.Above)
        # raise the focused fullscreen client last so it sits above the tiles
        if fs is not None and self.x.is_managed(fs) and self.x.focused == fs:
            self.x.set_stacking(fs, X.Above)

    # --- tasks ---

    def poke(self) -> None:
        """Request a resync. Safe from any handler, callback, or signal."""
        if self._dirty is not None:
            self._dirty.set()

    def _on_x_readable(self) -> None:
        """fd-reader callback: drain & translate X events into the queue.

        Synchronous on purpose — it empties the X socket so the fd stops being
        readable (no busy-spin), and translation that mutates frame state
        (unframe on Unmap/Destroy) happens here, before the async handler runs,
        exactly as the old single-loop version ordered it.
        """
        assert self._xq is not None
        while True:
            for ev in self.x.drain():
                self._xq.put_nowait(ev)
            if not self.x.has_pending():
                break
        self.x.flush()

    async def x_consumer(self) -> None:
        """Dispatch translated X events; poke a resync on layout changes."""
        assert self._xq is not None
        while True:
            ev = await self._xq.get()
            if ev.kind == "net_fullscreen":
                await self.on_net_fullscreen(ev.window, ev.arg)
            else:
                handler = self._dispatch.get(ev.kind)
                if handler is not None:
                    await handler(ev.window)
            if ev.kind in self._dirtying:
                self.poke()
            self.x.flush()

    async def nvim_task(self) -> None:
        """Keep the RPC connection alive: connect (retrying while nvim boots),
        learn our channel id, publish it, sync once, then sleep until the
        connection drops and reconnect."""
        self.rpc.on_notification = self._on_notification
        while True:
            try:
                await self.rpc.connect()
            except OSError:
                await asyncio.sleep(RECONNECT_DELAY_S)   # nvim not up yet
                continue
            try:
                info = await self.rpc.request("nvim_get_api_info")
                chan = info[0]
                if not await self.vim.publish_chan(chan):
                    raise ConnectionError("could not publish nvwm_chan")
                print(f"nvwm: connected to nvim (channel {chan})", file=sys.stderr)
                await self.vim.set_var("nvwm_mode", self.mode)
                self.poke()                              # fresh attach: sync once
                await self.rpc.wait_closed()
                print("nvwm: nvim closed the connection; reconnecting",
                      file=sys.stderr)
            except (ConnectionError, OSError, NvimError) as e:
                print(f"nvwm: nvim connection lost: {e!r}; reconnecting",
                      file=sys.stderr)
            finally:
                await self.rpc.aclose()
            await asyncio.sleep(RECONNECT_DELAY_S)

    def _on_notification(self, method: str, params: list[Any]) -> None:
        if method == "nvwm_dirty":
            self.poke()
        elif method == "nvwm_enter_gui":
            # neovim asked us to focus the current pane's GUI client. Runs as a
            # task because this callback is synchronous and must not block the
            # reader; the resync_task/x_consumer order is unaffected.
            asyncio.create_task(self._enter_gui())

    async def resync_task(self) -> None:
        """The only consumer of the dirty flag: debounce, then resync."""
        assert self._dirty is not None
        while True:
            await self._dirty.wait()
            await asyncio.sleep(DEBOUNCE_S)   # coalesce bursts (vnew, drags)
            self._dirty.clear()
            await self.resync()
            self.x.flush()

    async def main(self) -> None:
        if msgpack is None:
            sys.exit("nvwm: msgpack not importable (pacman -S python-msgpack)")
        loop = asyncio.get_running_loop()
        self._dirty = asyncio.Event()
        self._dirty.set()
        self._xq = asyncio.Queue()
        # SIGUSR1 -> out-of-band refresh, independent of the RPC channel:
        # `pkill -USR1 -f nvwm` from anywhere.
        loop.add_signal_handler(signal.SIGUSR1, self.poke)
        loop.add_reader(self.x.fileno(), self._on_x_readable)
        self._on_x_readable()                # drain anything already queued
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.x_consumer(),  name="x_consumer")
                tg.create_task(self.nvim_task(),   name="nvim_task")
                tg.create_task(self.resync_task(), name="resync_task")
        finally:
            loop.remove_reader(self.x.fileno())

    def run(self) -> None:
        asyncio.run(self.main())


if __name__ == "__main__":
    WindowManager().run()
