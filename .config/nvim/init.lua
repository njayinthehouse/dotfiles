vim.g.loaded_netrw = 1
vim.g.loaded_netrwPlugin = 1

-- Status bar shows the pane's memorable name (w:pane_id, set by nvwm.lua)
-- instead of the raw buffer name like "term://~//...". %{} is evaluated in
-- the context of each window, so every pane shows its own id.
-- laststatus=2 keeps a status line on every window (one per pane).
vim.o.laststatus = 2
vim.o.statusline = table.concat({
  " %{get(w:,'pane_id','[unnamed]')} ",      -- pane name, leading
  "%(· %{&buftype=='terminal' ? b:term_title : expand('%:t')} %)",  -- term/file
  "%=",                                       -- right-align the rest
  "%l:%c ",                                   -- line:col
})

-- nvim <dir> opens a terminal at that <dir>
vim.api.nvim_create_autocmd("VimEnter", {
  callback = function()
    local path = vim.api.nvim_buf_get_name(0)
    if vim.fn.isdirectory(path) == 1 then
      vim.cmd.cd(path)          -- cwd becomes the directory you passed
      vim.cmd.terminal()        -- replaces the dir buffer with a terminal
      vim.cmd.startinsert()     -- drop straight into terminal insert mode
    end
  end,
})

vim.keymap.set('x', '<C-S-c>', '"+y')                 -- copy visual selection
vim.keymap.set('n', '<C-S-v>', '"+p')                 -- paste (normal)
vim.keymap.set('i', '<C-S-v>', '<C-r><C-o>+')         -- paste (insert, literal)
vim.keymap.set('c', '<C-S-v>', '<C-r>+')              -- paste (cmdline)
vim.keymap.set('t', '<C-S-v>', function()             -- paste into a :ter's shell
    vim.fn.chansend(vim.b.terminal_job_id, vim.fn.getreg('+'))
  end)
