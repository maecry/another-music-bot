"""Play history for /wrapped, in data/history.db (SQLite, stdlib).

One row per track start (who requested it, who was in voice), plus the seconds actually played,
added each time playback of that track stops (skip, seek, end). Writes are tiny and synchronous.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

MIN_PLAY = 30  # seconds before a play counts (like Spotify)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    title TEXT NOT NULL,
    artist TEXT,
    url TEXT,
    thumbnail TEXT,
    requester_id INTEGER,
    played REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS plays_guild ON plays (guild_id, started_at);
CREATE TABLE IF NOT EXISTS listeners (
    play_id INTEGER NOT NULL REFERENCES plays(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (play_id, user_id)
);
CREATE INDEX IF NOT EXISTS listeners_user ON listeners (user_id);
"""


def clean_artist(name: str | None) -> str | None:
    """'Artist - Topic' / 'ArtistVEVO' / 'Artist Official' -> 'Artist'."""
    if not name:
        return None
    name = re.sub(r"\s*-\s*Topic$|VEVO$|\s+Official$", "", name.strip(), flags=re.I)
    return name.strip() or None


class PlayStats:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")

    def start(self, guild_id: int, track, listeners: list[int]) -> int | None:
        try:
            with self.db:
                cur = self.db.execute(
                    "INSERT INTO plays (guild_id, started_at, title, artist, url, thumbnail, requester_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (guild_id, int(time.time()), track.title, clean_artist(track.uploader),
                     track.url, track.thumbnail, track.requester_id),
                )
                self.db.executemany("INSERT OR IGNORE INTO listeners VALUES (?, ?)",
                                    [(cur.lastrowid, uid) for uid in listeners])
                return cur.lastrowid
        except sqlite3.Error as e:
            log.warning("stats: can't record play: %s", e)
            return None

    def add_time(self, play_id: int | None, seconds: float) -> None:
        if not play_id or seconds <= 0:
            return
        try:
            with self.db:
                self.db.execute("UPDATE plays SET played = played + ? WHERE id = ?", (seconds, play_id))
        except sqlite3.Error as e:
            log.warning("stats: can't update play: %s", e)

    def query(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def close(self) -> None:
        self.db.close()
