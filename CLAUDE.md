@AGENTS.md

## Claude Code notes
- Shell is Windows (PowerShell + Git Bash). Use the venv's interpreter: `.venv/Scripts/python`.
- SFTP to the host goes through helper scripts in the user's `~/.ssh/` (`apollo-sftp.sh`, which
  reads commands from stdin and gets the password via `apollo-askpass.sh` from `apollo.env`).
  Example: `printf 'put <local> <remote>\n' | ~/.ssh/apollo-sftp.sh`. Don't use `sftp -b` (it
  forces BatchMode and breaks password auth). **Never read or print `apollo.env`.**
- Remote paths are relative to the server root (e.g. `musicbot/player.py`, `logs/bot.log`).
  To debug, `get logs/bot.log` into the scratchpad and read it there.
- Ask the user to restart the server in the panel after uploading; you can't restart it yourself.
- Git repo: https://github.com/maecry/another-music-bot (`main`). Commit or push only when asked;
  no Co-Authored-By lines.
