-- nvwm.lua — neovim side of the nvwm notification system.
--
-- The WM connects over RPC and publishes its channel id as _G.nvwm_chan
-- (see wm.py / nvim_task). Anything that changes layout pokes the WM with an
-- 'nvwm_dirty' notification; the WM debounces (10ms) and resyncs. Without
-- this file the WM is deaf: it resyncs only on map/unmap/destroy and SIGUSR1.
--
-- Installed into ~/.config/nvim/plugin/ (auto-sourced) by install.sh.

-- Fire a dirty notification at the WM, if one is attached.
-- Global so external tools (pane swap) can reuse the same path.
function _G.nvwm_notify()
  local chan = _G.nvwm_chan
  if chan then
    local ok = pcall(vim.rpcnotify, chan, "nvwm_dirty")
    if not ok then
      -- channel died (WM restarting); it republishes on reconnect
      _G.nvwm_chan = nil
    end
  end
end

local grp = vim.api.nvim_create_augroup("nvwm", { clear = true })
vim.api.nvim_create_autocmd(
  -- every event that changes pane geometry:
  --   WinResized  — split separator drags, :resize, wincmd =  (covers
  --                 sibling windows too; fires once per batch since 0.9)
  --   WinNew      — new split created
  --   WinClosed   — split removed (remaining panes grow)
  --   VimResized  — host terminal itself resized
  { "WinResized", "WinNew", "WinClosed", "VimResized" },
  { group = grp, callback = _G.nvwm_notify }
)

-- Auto-name new panes/terminals with memorable words (instead of p1/p2). This
-- is the one chokepoint every creation path passes through: GUI windows (the
-- WM runs :vnew -> WinNew), `pane new` (also WinNew), and :terminal (TermOpen).
-- Names are kept unique across all live panes. `pane new <id>` sets w:pane_id
-- synchronously, so the deferred assignment below sees it and skips.
math.randomseed(os.time() + ((vim.loop and vim.loop.getpid and vim.loop.getpid()) or 0))

local WORDS = {
  "maple", "otter", "cobalt", "ember", "fern", "pine", "slate", "willow",
  "marble", "quartz", "basil", "cedar", "clover", "dune", "flint", "garnet",
  "hazel", "indigo", "jade", "kelp", "lime", "mango", "nova", "olive", "opal",
  "pearl", "quill", "reef", "sage", "teak", "umber", "violet", "wren", "zinc",
  "amber", "birch", "coral", "delta", "elm", "frost", "glade", "heron", "iris",
  "koi", "lark", "moss", "nest", "oak", "plum", "robin", "spruce", "tide",
  "vale", "wisp", "aspen", "brook", "crane", "dusk", "echo", "finch", "grove",
  "ivy", "lotus", "meadow", "north", "onyx", "petal", "ridge", "storm",
  "thorn", "vine", "wave", "brass", "cliff", "drift", "harbor", "lake", "mint",
  "pebble", "raven", "tern", "bramble", "cove", "fjord", "gale", "moor",
}

local function used_names()
  local used = {}
  for _, w in ipairs(vim.api.nvim_list_wins()) do
    local ok, id = pcall(vim.api.nvim_win_get_var, w, "pane_id")
    if ok and id and id ~= "" then used[id] = true end
  end
  return used
end

local function pick_name()
  local used = used_names()
  local free = {}
  for _, w in ipairs(WORDS) do
    if not used[w] then free[#free + 1] = w end
  end
  if #free > 0 then return free[math.random(#free)] end
  local n = 2                                   -- all words taken: suffix them
  while true do
    for _, w in ipairs(WORDS) do
      if not used[w .. n] then return w .. n end
    end
    n = n + 1
  end
end

local function autoname(win)
  if not vim.api.nvim_win_is_valid(win) then return end
  local ok, id = pcall(vim.api.nvim_win_get_var, win, "pane_id")
  if ok and id and id ~= "" then return end    -- explicit / already named
  vim.api.nvim_win_set_var(win, "pane_id", pick_name())
end

vim.api.nvim_create_autocmd({ "WinNew", "TermOpen" }, {
  group = grp,
  -- defer so the new window is current and an explicit `pane new <id>` wins
  callback = function()
    vim.schedule(function() autoname(vim.api.nvim_get_current_win()) end)
  end,
})

-- Manual refresh from inside neovim: :NvwmRefresh
vim.api.nvim_create_user_command("NvwmRefresh", _G.nvwm_notify, {})

-- Toggle fullscreen for the current pane's GUI client: :NvwmFull
-- (the WM toggles, so calling it on an already-full pane restores it). Bind it
-- to taste, e.g.  vim.keymap.set("n", "<leader>f", "<cmd>NvwmFull<cr>")
vim.api.nvim_create_user_command("NvwmFull", function()
  vim.g.nvwm_fullscreen_pending = vim.api.nvim_get_current_win()
  _G.nvwm_notify()
end, {})

-- Enter "GUI mode": in a pane backed by an X client, the placeholder neovim
-- window is empty, so `i` (and a/I/A) shouldn't open neovim's insert mode —
-- instead it hands the keyboard to the GUI client so its own keys work (arrows
-- scroll, etc.). The WM marks such windows with w:nvwm_gui = <client id> at map
-- time (see wm.py) and, on the 'nvwm_enter_gui' notification, focuses + raises
-- that client (see WindowManager._enter_gui). Ctrl+Esc round-trips back to
-- normal mode. Ordinary panes keep the default insert behaviour.
function _G.nvwm_enter_gui(fallback)
  local ok, cid = pcall(vim.api.nvim_win_get_var, 0, "nvwm_gui")
  if ok and cid then
    local chan = _G.nvwm_chan
    if chan and not pcall(vim.rpcnotify, chan, "nvwm_enter_gui") then
      _G.nvwm_chan = nil          -- channel died; republished on reconnect
    end
    return ""                     -- swallow the key: the WM moves focus
  end
  return fallback                 -- not a GUI pane: behave like the real key
end

for _, key in ipairs({ "i", "a", "I", "A" }) do
  vim.keymap.set("n", key, function() return _G.nvwm_enter_gui(key) end,
    { expr = true, desc = "nvwm: focus GUI client or " .. key })
end

-- Click-to-enter: a plain left click shouldn't just move focus to a pane, it
-- should drop straight into that pane's editing mode — mirroring how clicking a
-- GUI pane hands it the keyboard. neovim's default mouse handling selects the
-- clicked window on <LeftMouse>; we act on the matching <LeftRelease> (so a
-- click-drag that builds a visual selection is left untouched) and, for a
-- terminal or any modifiable buffer, enter Terminal-/Insert-mode. Non-editable
-- panes (help, quickfix, GUI placeholders, &c.) stay in normal mode. The WM's
-- WindowManager.on_clicked has already pulled X focus onto the nvim host.
vim.keymap.set("n", "<LeftRelease>", function()
  vim.schedule(function()
    if vim.fn.mode() ~= "n" then return end        -- a drag-selection: leave it
    local ok, gui = pcall(vim.api.nvim_win_get_var, 0, "nvwm_gui")
    if ok and gui then return end                  -- GUI placeholder: WM owns it
    local bt = vim.bo.buftype
    if bt == "terminal" or (bt == "" and vim.bo.modifiable) then
      vim.cmd("startinsert")                        -- terminal -> Terminal-mode,
    end                                             -- file buffer -> Insert-mode
  end)
  return "<LeftRelease>"
end, { expr = true, desc = "nvwm: click a pane to enter its editing mode" })
