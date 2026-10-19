#!/usr/bin/env bash
# One-command start for macOS / Linux. Creates .venv on first run, upgrades yt-dlp, then launches Recall.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "[recall] creating virtualenv (first run only)…"
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
  .venv/bin/pip install -r requirements.txt
fi
# TikTok changes often and yt-dlp fixes it within days: always run the newest yt-dlp (+ curl_cffi for impersonation).
# Set RECALL_NO_UPDATE=1 to skip (e.g. offline). Set RECALL_YTDLP_NIGHTLY=1 to use the nightly build.
if [ -z "${RECALL_NO_UPDATE:-}" ]; then
  echo "[recall] updating yt-dlp…"
  PRE=""; [ -n "${RECALL_YTDLP_NIGHTLY:-}" ] && PRE="--pre"
  .venv/bin/pip install -q --disable-pip-version-check -U $PRE "yt-dlp[default,curl-cffi]" \
    || echo "[recall] could not update yt-dlp (offline?) — continuing with $(.venv/bin/python -m yt_dlp --version 2>/dev/null || echo 'installed version')"
fi
echo "[recall] yt-dlp $(.venv/bin/python -m yt_dlp --version 2>/dev/null || echo '?')"
command -v ffmpeg >/dev/null || { echo "[recall] ffmpeg not found — install it (brew install ffmpeg / sudo apt install ffmpeg)"; exit 1; }
# fewer malloc arenas = lower resident memory on 8 GB machines
export MALLOC_ARENA_MAX=${MALLOC_ARENA_MAX:-2}
exec .venv/bin/python -m recall "$@"
