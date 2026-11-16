# Architecture

A single-process discord.py bot. The design goal is to run several servers' music on a **512 MB RAM,
slow-CPU** panel host, so most choices trade features for CPU and memory (see "Performance rules").

```
bot.py                 entry point: logging, PATH setup, davey check, MusicBot (commands.Bot)
run.py                 shim for panels whose Python template runs run.py -> bot.main()
start.sh               alternative startup for hosts that run a shell script (venv or --user)
musicbot/
  config.py            Config dataclass loaded from config.toml (+ DISCORD_TOKEN env override)
  settings.py          per-server settings (GuildSettings), stored in data/settings.json
  stats.py             PlayStats: play history (SQLite, data/history.db) for /wrapped and the quiz
  lunar.py             Vietnamese lunar calendar (Hồ Ngọc Đức algorithm, UTC+7)
  ffmpeg.py            finds ffmpeg (system, else imageio-ffmpeg) and deno; fixes PATH
  extractor.py         yt-dlp wrapper: Track dataclass, search/resolve/autocomplete, match cache
  sources.py           Spotify/Deezer link reader (no API keys) -> Tracks with a search_query
  cache.py             AudioCache: saves played Opus files to cache/audio, LRU + age cleanup
  player.py            GuildPlayer: queue, history, loop, filters, play loop, timers, panel
  ui.py                Components V2 panel (PanelView), queue pages, persistent buttons
  cogs/music.py        all music slash commands + /filter + /settings group
  cogs/admin.py        owner-only /stats, /sync, /update (git pull, pip if requirements changed, then bot.close + os.execv in main)
  cogs/general.py      /help (built from the command tree)
  cogs/community.py    /lunar, /setlunardayremind (hourly tasks.loop), ghost-ping tracker (/setghostping)
  cogs/wrapped.py      /wrapped: SQL over history.db -> paged LayoutView "story"
  cogs/quiz.py         /quiz start|stop, /guess: Heardle-style game with its own VoiceClient
```

Runtime folders (git-ignored): `cache/` (audio + `matches.json`), `data/` (settings, history.db), `logs/`.

## Startup (bot.py)
1. `setup_logging()`: rotating `logs/bot.log` (1 MB x 3) + console.
2. `prepare_path()` puts the venv / user-site `bin` on PATH so yt-dlp finds `deno`.
3. Logs the ffmpeg path, checks `davey` imports (and errors if davey 0.2.x is installed), checks deno.
4. `MusicBot.setup_hook`: registers the persistent `DynamicItem`s (`ControlButton`, `QueueSelect`),
   loads the cogs, adds alias commands, syncs. With `dev_guild_id` set, commands are copied to and
   synced on that guild only (instant); otherwise globally. Sync results fill `bot.command_ids`
   (used by /help for clickable mentions).

Intents: `guilds`, `voice_states`, `guild_messages` (for the ghost-ping tracker; mentions are sent
without the message-content intent). The member cache keeps voice members only. No message content,
no members intent. `MusicBot` owns the shared `settings` (SettingsStore) and `stats` (PlayStats).

## Playing a song
```
/play -> Music._play
   is_meta_url? -> MetaSources.fetch (Spotify/Deezer page/API -> Tracks with search_query)
   else         -> Extractor.search (URL or ytsearch1:/scsearch1:, playlists flat)
               (YouTube watch?v=X&list=Y: buttons ask first, "whole playlist" -> playlist?list=Y, X moved to the front)
   -> Music._connect (join voice, create GuildPlayer) -> player.enqueue
GuildPlayer._run (one asyncio task per guild, loops forever):
   _next_track()                    loop/history/previous handling
   _cached(track)                   apply saved Spotify->YouTube match, AudioCache.lookup
   not cached -> extractor.resolve  (Spotify tracks: _match = best of ytsearch3 by duration)
              -> track.wait_available()   YouTube URLs 403 until `available_at`
   vc.play(FFmpegOpusAudio(...), after=_after)
   new track -> _post_panel(), set_channel_status("🎶 title")
   not cached -> cache.store(track) in background (10 MB Range chunks)
   _prefetch() resolves the next queued track
   await _done (set from the ffmpeg `after` callback, which runs on another thread)
```
`skip()`, `seek()`, `previous()` and `set_filter()` all work by stopping the voice client; `_run`
then picks the next action from flags (`_skip`, `_seek_to`, `_back`).

## Track
`extractor.Track` (slots dataclass): `url` (page URL), `title`, `duration`, `thumbnail`, `uploader`,
`requester_id`, and resolved fields `stream_url`, `is_opus`, `headers`, `resolved_at`,
`available_at`. Spotify/Deezer tracks also carry `search_query` and, once matched, `play_url`
(the YouTube URL). `apply_stream()` keeps the Spotify title/artist when filling YouTube data.

## UI (Components V2)
- `PanelView` is a `ui.LayoutView`: Container (Section with Thumbnail, status line, `QueueSelect` jump + a second "play next" `QueueSelect` when 2+ queued)
  + two `ActionRow`s of `ControlButton`s. Components V2 messages cannot have `content` or embeds.
- Max 5 buttons per ActionRow, 25 select options.
- Buttons/select are `DynamicItem`s matched by custom_id (`music:<action>`, `music-jump`, `music-next`), so old
  panels keep working after a restart. All actions go through `ui.handle_control()`.
- A new track deletes the old panel and posts a new one, so it stays at the bottom of the chat.
  `refresh_panel()` edits in place (queue count, loop, pause, filter).

## Play history (stats.py)
`GuildPlayer._run` inserts a `plays` row when a new track starts (title, cleaned artist, url,
thumbnail, requester) plus one `listeners` row per non-bot member in the voice channel. When playback
of that track stops for any reason (end, skip, seek, destroy), the wall-clock time played is added to
`plays.played`. Queries count rows with `played >= MIN_PLAY` (30 s). Hours/days use UTC+7.

## Community features (cogs/community.py)
- Lunar reminders: `tasks.loop` fires at every full hour (UTC+7); if tomorrow is lunar day 1 or 15, every
  guild whose `lunar_hour` matches gets the message. Missed if the bot is down at that hour.
- Ghost pings: `on_message` keeps an in-memory `OrderedDict` of messages that mention users/roles/everyone
  (only for guilds with the tracker on; 10 min, max 5000). `on_raw_message_delete` / `on_raw_message_edit`
  compare against it. Bulk deletes (purges) are ignored.

## Music quiz (cogs/quiz.py)
One `Game` per guild with its own `VoiceClient` (refused while a `GuildPlayer` exists, and `/play` is
refused during a quiz). `_run` prepares the next song (audio cache lookup or `extractor.resolve`) while
the current round plays. A round walks `STAGES` (1/5/10/20 s clips from a random point 15-40% into the
song, ffmpeg `-ss` + `-t`, Opus copy); `game.advance` is set by Skip or a right `/guess`, otherwise each
stage times out. `/guess` autocomplete returns `#q<index>` values from the song list; free text is
matched after `norm()` (accents stripped).

## Per-server settings
`SettingsStore` holds one mutable `GuildSettings` per guild. It lives on the bot (`bot.settings`) and
is shared by all cogs; `/settings reset` only resets the music fields (`MUSIC_SETTINGS`). `GuildPlayer.settings` is the same
object, so `/settings edit` applies live. `None` timeouts fall back to `config.toml`; `0` means never
leave. Only non-default entries are written, atomically (tmp file + `os.replace`).

## Performance rules (don't break these)
- **No re-encoding by default.** YouTube audio is Opus, so ffmpeg runs with `-c:a copy`. Only
  non-Opus sources and `/filter` presets use libopus (`FILTERS` in player.py lists the chains and
  playback speeds; `position` multiplies elapsed time by the speed).
- **No `/volume`**, because it would force re-encoding of every stream.
- **One yt-dlp extraction at a time** (semaphore), in a thread. Each extraction can spawn deno.
- **Lazy resolution:** playlists are stored flat; a stream URL is resolved just before playing,
  plus one prefetch.
- **Audio cache** replaces repeated lookups with a local file (copy codec, `-nostdin`).
- **Quiz clips** are short ffmpeg copy runs; one yt-dlp resolve per round, prefetched.
- Keep new features cheap: polling feeds and small JSON/SQLite files are fine; image rendering,
  headless browsers, JVMs (Lavalink) and web dashboards are not.
