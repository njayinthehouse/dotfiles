vim.g.loaded_netrw = 1
vim.g.loaded_netrwPlugin = 1

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
