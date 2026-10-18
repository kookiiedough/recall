#!/usr/bin/env bash
# One-command start for macOS / Linux. Creates .venv on first run, then launches Recall.
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
command -v ffmpeg >/dev/null || { echo "[recall] ffmpeg not found — install it (brew install ffmpeg / sudo apt install ffmpeg)"; exit 1; }
# fewer malloc arenas = lower resident memory on 8 GB machines
export MALLOC_ARENA_MAX=${MALLOC_ARENA_MAX:-2}
exec .venv/bin/python -m recall "$@"
