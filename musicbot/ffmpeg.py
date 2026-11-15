"""Locate the external binaries (ffmpeg, deno) without needing root/apt."""
import os
import shutil
import site
import sys
from pathlib import Path


def prepare_path() -> None:
    """Put pip-installed binaries (venv Scripts/bin, ~/.local/bin) on PATH so yt-dlp finds deno."""
    candidates = [Path(sys.executable).parent, Path(site.getuserbase()) / ("Scripts" if os.name == "nt" else "bin")]
    path = os.environ.get("PATH", "")
    for c in candidates:
        if c.is_dir() and str(c) not in path:
            path = f"{c}{os.pathsep}{path}"
    os.environ["PATH"] = path


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError:
        raise SystemExit("ffmpeg not found: install it or `pip install imageio-ffmpeg`.")
    return imageio_ffmpeg.get_ffmpeg_exe()


def check_deno() -> str | None:
    return shutil.which("deno")
