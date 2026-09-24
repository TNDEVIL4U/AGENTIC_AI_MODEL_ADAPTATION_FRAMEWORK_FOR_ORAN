# CLAUDE.md — Safety & Resource Rules

This machine is a **low-end laptop: 16 GB RAM, no GPU**. It has crashed (blackout/restart) during
earlier sessions and has shown "OS file deleted / not found" errors. Treat the system as fragile.
**Safety and stability come before speed or completeness.**

---

## 1. Golden rules

1. **Do no harm to the system.** Never take an action that could damage the OS, delete user data,
   or change system settings.
2. **Be precise.** Only touch the files needed for the current task. No broad or "cleanup" changes
   unless explicitly asked.
3. **When unsure, stop and ask.** Do not guess on anything destructive or irreversible.
4. **Explain before acting.** Before running any command that changes files, state what it will do
   and which files it affects.

---

## 2. Forbidden actions (never do these)

- Never delete files or folders outside this project directory.
- Never use recursive/force deletes (`rm -rf`, `del /s /q`, `rmdir /s`, `Remove-Item -Recurse -Force`).
- Never touch system locations:
  - Windows: `C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`, `C:\ProgramData`, the registry
  - Linux/macOS: `/`, `/bin`, `/boot`, `/etc`, `/lib`, `/usr`, `/var`, `/System`, `/Library`
- Never use `sudo`, "Run as administrator", `chmod 777`, or change file ownership.
- Never modify, kill, or restart system services or other running processes
  (`kill`, `taskkill`, `pkill`, `systemctl`, `sc`, `shutdown`, `reboot`).
- Never format, partition, or write directly to disks (`format`, `diskpart`, `mkfs`, `dd`, `fdisk`).
- Never edit environment variables, PATH, startup items, drivers, firewall, or antivirus settings.
- Never read or print secrets: `.env` files, SSH keys (`~/.ssh`), passwords, tokens, browser data,
  credential stores.
- Never run `git push --force`, `git reset --hard`, or `git clean -fd` without explicit permission.
- Never download and run unknown scripts (`curl ... | sh`, `iwr ... | iex`).
- Never install global packages (`npm -g`, `pip install` outside a virtual env) without asking.

---

## 3. Actions that require asking first

- Deleting, moving, or renaming **any** file (even inside the project).
- Installing or upgrading packages.
- Running builds, test suites, or dev servers.
- Any command expected to run longer than ~1 minute.
- Any change to more than 5 files at once.

---

## 4. Resource limits (RAM / CPU / disk)

The laptop crashes when overloaded. Keep usage light at all times.

**Memory**
- Run **one heavy process at a time.** Never run builds, tests, and servers in parallel.
- Do not spawn subagents or parallel tasks unless explicitly asked.
- Read files in parts (use offset/limit) instead of loading very large files whole.
- Never load an entire large dataset into memory; process in chunks or stream it.
- Node: cap memory, e.g. `NODE_OPTIONS=--max-old-space-size=2048`.
- Python: avoid loading huge files with `read()`; iterate line by line or use chunked readers.

**CPU**
- Limit parallel workers:
  - `npm`/`jest`: `--maxWorkers=2` (or `--runInBand`)
  - `pytest`: no `-n auto`; use `-n 2` at most
  - `make`: `-j2` at most
  - Build tools (webpack/vite/gradle/cargo): 2 workers/jobs max
- Never assume a GPU exists. Use CPU-only settings for any ML library (e.g. `device="cpu"`),
  and never train or run large models locally.

**Disk & search**
- Keep searches narrow: search specific folders, not the whole drive.
- Always exclude heavy folders from searches: `node_modules`, `.git`, `venv`, `.venv`,
  `dist`, `build`, `__pycache__`.
- Never scan the whole home directory or system drive.
- Do not create large temporary files; clean up only the temp files **you** created.

**Processes**
- Do not leave background processes running. Stop any server/watcher you started when done.
- Never kill processes you did not start.

---

## 5. Handling errors

- If you see "file not found" or "file deleted" errors: **do not try to fix it by recreating,
  deleting, or moving system files.** Report the exact path and error to the user and stop.
- If a command fails, do not retry it repeatedly in a loop. Retry at most once, then report.
- If memory or CPU seems high (slow responses, timeouts), stop and tell the user rather than
  pushing on.

---

## 6. Working style

- Make small, focused edits. Prefer editing a file over rewriting it entirely.
- Show a short plan before multi-step changes.
- After finishing, summarise which files changed.
