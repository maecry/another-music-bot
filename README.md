# Another Music Bot

A lightweight, self-hosted Discord music bot.

Slash commands, Components V2 now-playing panel with buttons, built for small hosts
(512 MB RAM, slow CPU). Python + discord.py 2.7 (DAVE encrypted voice) + yt-dlp.

## Setup
```
git clone https://github.com/maecry/another-music-bot
cd another-music-bot
```
1. Create a bot at https://discord.com/developers/applications → **Bot** → copy the token.
   No privileged intents are needed.
2. Invite it: OAuth2 → URL Generator → scopes `bot` + `applications.commands`,
   permissions **View Channels, Send Messages, Send Messages in Threads, Embed Links, Read Message
   History, Connect, Speak, Set Voice Channel Status** (the last one shows the song as the voice
   channel's status; optional).
3. `cp config.example.toml config.toml` and paste the token (or set `DISCORD_TOKEN`).
   Set `dev_guild_id` to your server ID while testing so commands also appear there instantly.

### Panel host (SFTP)
Upload the folder (without `.venv`), set the startup command to `bash start.sh`, start.

If your panel's Python egg runs `git pull` on startup (the usual Pterodactyl one does when a
`.git` folder exists), point it at this repo instead and set the startup file to `run.py`. Every
restart then pulls the latest code and reinstalls `requirements.txt`. Upload `config.toml` yourself:
it's git-ignored.
The script installs everything with pip (ffmpeg and deno come as pip packages if the host
doesn't have them) and upgrades yt-dlp on every boot.

### Locally
```
python -m venv .venv
.venv\Scripts\activate        # Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m musicbot.extractor "test song"   # checks yt-dlp + deno work
python bot.py
```
Python 3.11+ required.

## Sources
- **YouTube, SoundCloud, Bandcamp, Twitch, Vimeo, Mixcloud, direct mp3/ogg links** and
  [anything else yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md):
  paste the link (songs, playlists, SoundCloud sets). A YouTube link to a song inside a playlist
  asks whether to queue the whole playlist (that song first, then the rest) or just that song.
  YouTube Mixes (`list=RD...`) work the same way, capped at 50 songs.
- **Spotify and Deezer** links (track, album, playlist, Spotify artist, share links): their audio is
  DRM-locked, so the bot reads the song list and plays the matching YouTube upload (the closest
  length out of the top 3 results). No API key needed. Spotify links show up to about 100 songs per playlist.
- **Search**: `/play query:<text>` searches YouTube; add `source:SoundCloud` to search SoundCloud instead.
- Apple Music isn't supported: it has no public song list to read.

## Commands
`/play` (with search suggestions; a pasted link shows its title so you can check it) · `/playnext` (adds to the front of the queue) · `/skip` `/previous`
`/pause` `/resume` `/stop` `/leave` `/join`
· `/queue` `/nowplaying` `/shuffle` `/loop` `/remove` `/move` `/seek` `/clear` `/dedupe`
· `/filter`: Bass boost, Nightcore, Vaporwave, Slowed + reverb, 8D, Karaoke, Treble boost. Filters re-encode
the audio (~5-10% of a CPU core per playing server), so leave them Off when not wanted.
· `/settings view|edit|reset` (Manage Server): default search source, idle/alone timeouts (0 = never
leave, i.e. 24/7), and a fixed channel for the now-playing panel. Saved in `data/settings.json`.
· owner only: `/stats` `/sync` `/update` (git pull + restart the bot, no panel restart needed). Owner = the app owner plus `owner_ids` in config.toml

**Fun & community**
- `/quiz start [playlist] [rounds]`: guess-the-song in your voice channel, Heardle style. Each round
  starts with a **1 s** clip (Impossible, 4 pts); ⏭️ Skip unlocks **5 s** (Hard, 3), **10 s** (Medium, 2),
  **20 s** (Easy, 1), then the answer is revealed. Each stage also auto-skips after 20 s. Answer with
  `/guess`, which searches the quiz's song list as you type. One guess per person per stage. Songs come
  from a playlist link, or by default from the server's most played songs. `/quiz stop` ends it (host or
  Manage Server). Music can't play during a quiz.
- `/wrapped [user] [period] [server]`: Spotify-Wrapped-style recap you flip through: minutes listened,
  top songs and artists, peak hour, streaks, listening buddy, personality, and a summary card. `server:True`
  shows the whole server (top listeners, top DJs). A song counts after 30 s of playing for everyone in
  the voice channel at the time. History is kept in `data/history.db` (starts empty, from this update on).
- `/lunar`: today's Vietnamese lunar date (âm lịch) and the next mùng 1 / Rằm.
- `/setlunardayremind [channel] [hour] [ping] [off]` (Manage Server): posts the day before every lunar
  1st and 15th, at `hour` Vietnam time (default 20:00), e.g. "🌕 Ngày mai là Rằm tháng Bảy (Lễ Vu Lan)".
  Pinging @everyone or a non-mentionable role needs the bot's **Mention Everyone** permission.
- `/setghostping enabled [channel]` (Manage Server): when someone pings a person/role and deletes the
  message, or edits the ping out, within 10 minutes, the bot posts who pinged whom (and re-pings the
  victims). It can't show the message text (no message-content intent). A mod deleting a message with a
  ping also gets reported.

Shuffle: `/shuffle` or the 🔀 button. Repeat: `/loop` (alias `/repeat`) with Off / Current track /
Whole queue, or the 🔁 button to cycle.

**Aliases**: extra names for commands, set under `[aliases]` in `config.toml`. Defaults:
`/next`→skip, `/repeat`→loop, `/p`→play, `/np`→nowplaying, `/q`→queue, `/disconnect`→leave, `/back`→previous.
Restart the bot after changing them. Aliases are real slash commands, so they show in the `/` menu.

Panel buttons: ⏮️ previous · ⏸️ pause/resume · ⏭️ skip · 🔁 loop · 🔀 shuffle · ⏹️ stop, plus a queue dropdown.
They keep working after a restart.

## Why it's light
- YouTube audio is already Opus, so ffmpeg just copies it (no re-encoding, ~1% CPU).
- **Audio cache**: each song (YouTube Opus, up to 15 min) is saved to `cache/audio/` after its first
  play; later plays come from disk with no lookup or download. Songs not played for 7 days are
  deleted automatically, and the oldest go first above `cache_size_gb` (default 3 GB). Spotify/Deezer →
  YouTube matches are remembered in `cache/matches.json`. Set `cache_size_gb = 0` to turn it off.
- First plays stream; nothing is downloaded before playback starts. Playlists store titles only; each song is resolved right before it plays.
- Only the `guilds` + `voice_states` intents; no member/message caches.
- Leaves after 5 min idle or 3 min alone. Queue capped at 500 per server.
- No `/volume` on purpose (it would force re-encoding). Right-click the bot → User Volume instead.

## Troubleshooting
- **"Sign in to confirm you're not a bot"**: YouTube is blocking the host's IP. Export
  `cookies.txt` from a throwaway Google account (browser extension "Get cookies.txt LOCALLY"),
  upload it, and set `cookies_file = "cookies.txt"`.
- **Songs suddenly stop working**: restart (start.sh upgrades yt-dlp).
- **Bot joins but no sound**: if the bot's avatar lights up green but nobody hears anything, check
  that `davey` is below 0.2 (`requirements.txt` pins it; davey 0.2 + discord.py 2.7.1 = silence).
  Otherwise the host must allow outbound UDP; check `logs/bot.log`.
- **"davey failed to load"** / "Voice isn't available": discord.py needs `davey` for Discord's
  encrypted voice. On Windows, Smart App Control blocks it (Windows Security → App & browser
  control → Smart App Control). Linux hosts aren't affected.
- **`deno not found` warning**: `pip install deno`, needed for YouTube since yt-dlp 2025.11.

## For developers
Code layout and design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Rules for AI coding agents: [AGENTS.md](AGENTS.md).
