# AGENTS.md

Guide for AI coding agents working on this repo. Users read `README.md`; the design is in
`docs/ARCHITECTURE.md`. Read that before changing `player.py`, `ui.py` or `cogs/music.py`.

## What this is
A self-hosted Discord bot for 1-3 small servers, music first, with more general features planned
(e.g. game-update trackers). Python 3.11+ (host runs 3.12), discord.py 2.7 with DAVE encrypted
voice, yt-dlp + deno for YouTube, ffmpeg. Host: Pterodactyl-style panel, **512 MB RAM, 10 GB disk,
slow CPU**, no root, files uploaded over SFTP.

## Commands
```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt   # Windows dev setup
.venv\Scripts\python bot.py                                             # run (needs config.toml)
.venv\Scripts\python -m musicbot.extractor "some song"                  # yt-dlp/deno self-test
```
There is no test suite or linter config. Verify changes with small throwaway scripts using the
venv's Python, put them outside the repo (e.g. a temp dir) and `sys.path.insert(0, ".")`. Patterns that
have worked:
- **Queue logic:** build a player with `GuildPlayer.__new__(GuildPlayer)`, set the attributes the
  method needs, and call `_next_track()`, `previous()`, `dedupe()` etc. directly.
- **Views:** call `PanelView(fake_player).to_components()`.
- **Commands:** construct `MusicBot(Config(token="x"))`, `load_extension` each cog, `add_aliases()`,
  then inspect `bot.tree.get_commands()`. Close the cog's aiohttp session at the end.
- **ffmpeg filters:** run each chain on `-f lavfi -i sine=...` and check the exit code.

On Windows, set `PYTHONIOENCODING=utf-8` when printing emoji.

## Hard rules
- **Never print or commit secrets.** `config.toml` holds the bot token; `cookies.txt` may exist.
  Both are git-ignored. To change one key, edit that line (e.g. `sed` on `dev_guild_id`) rather
  than reading the file out.
- **Keep it light.** No re-encoding on the default path (Opus `-c:a copy`), one yt-dlp extraction
  at a time, no Lavalink/JVM/browser/image rendering. Mention the CPU/RAM cost of any feature that
  adds some (filters are the accepted exception and are opt-in).
- **`davey` must stay `<0.2`** until discord.py supports it (Rapptz/discord.py#10548). With davey
  0.2.x the bot connects and "plays" but listeners hear silence, and the logs look clean.
- Components V2: no `content`/embeds on `LayoutView` messages, max 5 buttons per row, 25 select
  options, 4000 chars of text per message. Persistent controls must be `DynamicItem`s registered in
  `MusicBot.setup_hook`.
- Interactions must be answered within 3 s; defer before any yt-dlp or network work.
- The voice `after` callback runs on another thread. Use `loop.call_soon_threadsafe`.
- New slash commands: put them in the right category in `cogs/general.py` `CATEGORIES` (unlisted
  ones fall under "Other" in /help). Per-server options belong in `settings.py` `GuildSettings`.
  Global options belong in `config.py` + `config.example.toml`.
- New cogs: add the extension name to the tuple in `MusicBot.setup_hook`.
- Update `README.md` (user docs) and `docs/ARCHITECTURE.md` when behaviour or structure changes.

## Known gotchas
- YouTube stream URLs return 403 until `available_at` (~4-5 s after resolve). That's why
  `Track.wait_available()` exists. Don't remove it.
- YouTube needs deno (yt-dlp-ejs). `ffmpeg.prepare_path()` makes the pip-installed deno findable.
- Windows Smart App Control blocks davey's native module, so **voice can't be tested on the
  owner's PC**. Login, commands, extraction and ffmpeg can be. Voice is tested on the host.
- The host's startup command runs `pip install -U --prefix .local -r requirements.txt` then
  `python3 run.py`, so dependency pins in `requirements.txt` apply on the next restart.
- Slash commands always sync globally (every server, up to an hour to appear). `dev_guild_id` adds a
  guild copy so new commands show instantly in that one server.
- Bot permissions currently needed: View Channels, Send Messages, Send Messages in Threads, Embed
  Links, Read Message History, Connect, Speak, Set Voice Channel Status (optional: Move Members).
  New features needing more must say so.

## Deploying
The server's `/home/container` is a git checkout of `main`, and its startup command runs `git pull`
first. So deploying = push to `main`, then the user restarts the server in the panel. Don't upload
tracked files over SFTP any more: a local edit on the server makes `git pull` fail. `config.toml`,
`cookies.txt`, `cache/`, `logs/`, `data/` and `.local/` are git-ignored and stay as they are on the
server. After a restart, check `logs/bot.log` on the server for `synced N slash commands` and
`logged in as ...`.
