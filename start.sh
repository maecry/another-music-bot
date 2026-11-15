#!/usr/bin/env bash
# Startup for a panel host (Pterodactyl-style): set the startup command to `bash start.sh`.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}

# Use a venv when possible, otherwise fall back to a --user install (no root needed either way).
if [ ! -x .venv/bin/python ]; then
  $PY -m venv .venv 2>/dev/null || true
fi
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python; PIPFLAGS=""
else
  PIPFLAGS="--user"; export PATH="$HOME/.local/bin:$PATH"
fi

$PY -m pip install -q --disable-pip-version-check $PIPFLAGS -r requirements.txt
# YouTube changes often; a fresh yt-dlp on every boot fixes most "it stopped playing" issues.
$PY -m pip install -q --disable-pip-version-check $PIPFLAGS -U "yt-dlp[default]"

exec $PY bot.py
