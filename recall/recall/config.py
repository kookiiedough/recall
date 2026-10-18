"""Paths and runtime configuration. Everything lives under RECALL_HOME (default ~/.recall)."""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    p = Path(os.environ.get("RECALL_HOME", Path.home() / ".recall")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home() / "recall.db"


def media_dir() -> Path:
    p = home() / "media"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tmp_dir() -> Path:
    p = home() / "tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def exports_dir() -> Path:
    p = home() / "exports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def models_dir() -> Path:
    p = Path(os.environ.get("RECALL_MODELS", home() / "models")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(p / "hf"))
    return p


# Default user-facing settings (stored/overridden in the DB `settings` table).
DEFAULT_SETTINGS = {
    "deep_index": True,        # run wave 2 at all
    "transcripts": True,       # faster-whisper
    "ocr": True,               # RapidOCR on sampled frames
    "visual": True,            # CLIP keyframe embeddings
    "text_embeddings": True,   # MiniLM chunk embeddings
    "sound": True,             # index sound / track metadata
    "whisper_model": "base",   # tiny | base
    "frame_interval": 2.0,     # seconds between sampled frames (OCR)
    "max_keyframes": 8,        # frames kept on disk + CLIP-embedded (plus thumbnail)
    "batch_size": 4,           # videos per wave-2 batch (models run sequentially per batch)
    "oembed_rate": 1.0,        # seconds between oEmbed requests
    "worker_paused": False,
}
