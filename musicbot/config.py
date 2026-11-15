import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALIASES = {
    "skip": ["next"],
    "loop": ["repeat"],
    "play": ["p"],
    "nowplaying": ["np"],
    "queue": ["q"],
    "leave": ["disconnect"],
    "previous": ["back"],
}


@dataclass(frozen=True)
class Config:
    token: str
    dev_guild_id: int = 0
    cookies_file: str = ""
    max_queue: int = 500
    idle_timeout: int = 300
    alone_timeout: int = 180
    autocomplete: bool = True
    accent_color: int = 0x5865F2
    cache_size_gb: float = 3.0     # audio cache cap on disk; 0 disables the cache
    cache_days: int = 7            # delete cached songs not played for this many days
    cache_max_minutes: int = 15    # don't cache longer tracks (mixes, podcasts)
    aliases: dict = field(default_factory=lambda: dict(DEFAULT_ALIASES))


def load(path: Path = ROOT / "config.toml") -> Config:
    data = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
    known = {k: v for k, v in data.items() if k in Config.__dataclass_fields__}
    known["token"] = os.environ.get("DISCORD_TOKEN") or known.get("token", "")
    if not known["token"] or known["token"] == "PASTE_BOT_TOKEN_HERE":
        raise SystemExit("No bot token: set `token` in config.toml or the DISCORD_TOKEN env var.")
    if known.get("cookies_file"):
        cookie = Path(known["cookies_file"])
        known["cookies_file"] = str(cookie if cookie.is_absolute() else ROOT / cookie)
    return Config(**known)
