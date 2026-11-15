"""Per-server settings, saved to data/settings.json. Players hold the live object, so edits apply at once."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class GuildSettings:
    default_source: str = "youtube"    # /play search source when none is picked
    idle_timeout: int | None = None    # seconds; None = config.toml value, 0 = never leave
    alone_timeout: int | None = None   # seconds; None = config.toml value, 0 = never leave
    panel_channel_id: int = 0          # post the now-playing panel here (0 = where /play was used)
    # lunar-day reminders (cogs/community.py)
    lunar_channel_id: int = 0          # 0 = off
    lunar_hour: int = 20               # Vietnam time (UTC+7), the day before
    lunar_role_id: int = 0             # role to ping (guild id = @everyone), 0 = no ping
    # ghost-ping tracker (cogs/community.py)
    ghostping: bool = False
    ghostping_channel_id: int = 0      # 0 = report in the channel where it happened

    def reset(self, names: tuple[str, ...] | None = None) -> None:
        for f in fields(self):
            if names is None or f.name in names:
                setattr(self, f.name, f.default)


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[int, GuildSettings] = {}
        try:
            raw = json.loads(path.read_text("utf-8"))
            known = {f.name for f in fields(GuildSettings)}
            for gid, values in raw.items():
                self._data[int(gid)] = GuildSettings(**{k: v for k, v in values.items() if k in known})
        except FileNotFoundError:
            pass
        except (ValueError, TypeError) as e:
            log.error("settings file %s is broken, using defaults: %s", path, e)

    def get(self, guild_id: int) -> GuildSettings:
        return self._data.setdefault(guild_id, GuildSettings())

    def items(self) -> list[tuple[int, GuildSettings]]:
        return list(self._data.items())

    def peek(self, guild_id: int) -> GuildSettings | None:
        """Like get() but doesn't create an entry (for hot paths such as on_message)."""
        return self._data.get(guild_id)

    def save(self) -> None:
        default = asdict(GuildSettings())
        out = {str(g): asdict(s) for g, s in self._data.items() if asdict(s) != default}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, indent=2), "utf-8")
        os.replace(tmp, self.path)  # atomic: a crash mid-write can't corrupt the file
