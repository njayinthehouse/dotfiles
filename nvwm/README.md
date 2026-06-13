# nvwm

A tiling X11 window manager whose layout *is* neovim's window layout. Every
GUI client lives inside a neovim split; as you resize, move, and close splits
in neovim, the GUI windows follow. neovim is the single source of truth for
geometry — nvwm just paints pixels onto its cell grid.

## Layout

| File        | Installed to                                                  |
|-------------|---------------------------------------------------------------|
| `wm.py`     | `~/.local/lib/wm.py` — `NvimRPC`, `VimTalker`, `XTalker`, `WindowManager` |
| `nvwm.py`   | `~/.local/bin/nvwm` — WM entry point (run by `.xinitrc`)      |
| `pane.py`   | `~/.local/bin/pane` — pane CLI                                |
| `nvwm.lua`  | `~/.config/nvim/plugin/nvwm.lua` — auto-sourced plugin        |
| `install.sh`| copies the files above into place                             |

The repo is the single source of truth; `install.sh` copies (not symlinks)
into the live locations, and `.zshrc` re-runs it whenever the repo changes.

## Design

One asyncio event loop, fully coroutine-based. A single `NvimRPC` connection
(minimal async msgpack-rpc, no pynvim) multiplexes both queries and
`nvwm_dirty` notifications. Three tasks under one `TaskGroup`:

- **x_consumer** — dispatches X events (drained into a queue by the fd reader)
- **nvim_task** — keeps the RPC connection alive, publishes `_G.nvwm_chan`
- **resync_task** — debounces the dirty flag and repositions GUI placements

Earlier versions drove a synchronous `pynvim` session inside the loop, which
raised *"Cannot run the event loop while another loop is running"*. Dropping
pynvim for a single async connection removes that whole class of bug.

## Install

```sh
sh install.sh
```

Then restart X (or `:NvwmRefresh` for a resync). For safe iteration without
touching your live session, run it nested in Xephyr via `~/run-dev.sh`.

## Refresh paths

- `:NvwmRefresh` inside neovim
- `pkill -USR1 -f nvwm` (works even if the RPC channel is wedged)

## pane CLI

Run inside a neovim `:terminal`:

```
pane new {<id>}          create a new pane (auto-named if no id)
pane ls {<regex>}        list panes, optionally filtered
pane swap <id1> {<id2>}  swap two panes, GUI included
pane kill {<id>}         close a pane
pane goto {<id>}         jump to a pane (or the last visited)
```

## Known issues

- **Firefox tab drag-to-other-window doesn't move the tab.** Dragging a tab
  onto another Firefox window's tab strip fails to merge it. Two fixes already
  in `wm.py` did *not* solve it: (1) a synthetic `ConfigureNotify` carrying root
  coordinates in `_notify_configure`, so reparented clients know their on-root
  position for hit-testing; (2) `button_pressed()` suppressing focus-follows-
  mouse mid-drag in `on_entered`. Both Firefox client windows do have
  `XdndAware` set. Next step: inspect the reparented tree (`xwininfo -root
  -tree`) and check whether XDND target-finding descends into the frame or needs
  an `XdndProxy` on the frame pointing at the client; also whether the absence
  of root EWMH (`_NET_CLIENT_LIST`) matters.
- `pane_to_pixels` alignment — the GUI slightly covers the status bar (needs a
  terminal padding offset).

## Requirements

`python-xlib`, `python-msgpack` (Python 3.11+ for `asyncio.TaskGroup`).
