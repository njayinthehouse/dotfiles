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
from Xlib import X
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

    def frame(self, client: XWindow, x: int, y: int, w: int, h: int) -> None:
        """Create a frame at (x, y, w, h) and reparent the client into it."""
        if client.id in self._frames:
            return
        frm: XWindow = self.root.create_window(
            x, y, max(1, w), max(1, h), 0,
            self.screen.root_depth, X.InputOutput, X.CopyFromParent,
            background_pixel=self.screen.black_pixel,
            event_mask=(X.SubstructureRedirectMask
                        | X.SubstructureNotifyMask | X.EnterWindowMask))
        self._ignore_unmaps += 1
        try:
            client.reparent(frm, 0, 0)
        except BadWindow:
            frm.destroy()
            return
        client.configure(width=max(1, w), height=max(1, h))
        client.change_attributes(event_mask=X.EnterWindowMask)
        frm.map()
        client.map()
        self._frames[client.id] = frm
        self._clients[frm.id] = client
        self._notify_configure(client, x, y, max(1, w), max(1, h))

    def unframe(self, client_id: int) -> None:
        frm = self._frames.pop(client_id, None)
        if frm is None:
            return
        self._clients.pop(frm.id, None)
        if self._focused == client_id:
            self._focused = None
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

    def button_pressed(self) -> bool:
        """True if any pointer button is held — i.e. a drag is in progress.
        Used to suppress focus-follows-mouse so we don't abort an app's own
        drag (notably Firefox tab tear-off/merge between windows)."""
        try:
            return bool(self.root.query_pointer().mask
                        & (X.Button1Mask | X.Button2Mask | X.Button3Mask))
        except Exception:
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
        elif ev.type == X.EnterNotify:
            client = self._resolve(ev.window)
            if client is not None:
                return WMEvent("entered", client)
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

    def _resolve(self, window: XWindow) -> XWindow | None:
        if window.id in self._clients:
            return self._clients[window.id]
        if window.id in self._frames:
            return window
        return None


# ============================================================================
#  WindowManager — glue
# ============================================================================

class WindowManager:

    def __init__(self) -> None:
        self.rpc = NvimRPC(NVIM_SOCK)
        self.vim = VimTalker(self.rpc)
        self.x = XTalker()
        self.x.claim_wm()
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
            "entered":     self.on_entered,
            "unmapped":    self.on_unmapped,
            "destroyed":   self.on_destroyed,
        }
        # event kinds that change layout and therefore warrant a resync
        self._dirtying: frozenset[str] = frozenset(
            {"map_request", "unmapped", "destroyed"})

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
            self.x.set_stacking(client.id, X.Above)
        else:
            self.x.set_stacking(client.id, X.Above)

    async def on_mapped(self, client: XWindow) -> None:
        self.x.focus(client.id)

    async def on_entered(self, client: XWindow) -> None:
        # Don't steal focus / restack while a button is held: focus-follows-
        # mouse firing mid-drag aborts app drags like Firefox tab tear-off.
        if self.x.button_pressed():
            return
        self.x.focus(client.id)
        if client.id != self.nvim_host_id:
            self.x.set_stacking(client.id, X.Above)

    async def on_unmapped(self, client: XWindow) -> None:
        await self._forget(client.id)

    async def on_destroyed(self, client: XWindow) -> None:
        await self._forget(client.id)

    async def _forget(self, client_id: int) -> None:
        vim_win = self.placements.pop(client_id, None)
        if vim_win is not None:
            await self.vim.close_pane(vim_win)
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

    async def resync(self) -> None:
        if self.host_geom is None:
            return
        await self._check_swap_signal()
        for cid, vim_win in list(self.placements.items()):
            if not self.x.is_managed(cid):
                self.placements.pop(cid, None)
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
