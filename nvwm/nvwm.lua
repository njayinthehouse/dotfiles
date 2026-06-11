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

-- Manual refresh from inside neovim: :NvwmRefresh
vim.api.nvim_create_user_command("NvwmRefresh", _G.nvwm_notify, {})
