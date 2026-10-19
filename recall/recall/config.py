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
    # --- wave-2 downloads (yt-dlp) ---
    "video_downloads": True,   # off = deep-index from the oEmbed thumbnail only (CLIP + OCR), never download
    "cookies_browser": "none",  # none | chrome | edge | firefox | safari | brave  -> yt-dlp --cookies-from-browser
    "cookies_file": "",        # optional Netscape cookies.txt path (overrides cookies_browser)
    "ytdlp_impersonate": "auto",  # auto (try plain desktop UA, then curl_cffi impersonation) | always | never
    "ytdlp_user_agent": "",    # optional User-Agent override for TikTok requests
    "download_retry_delay_s": 600,  # first automatic retry of a failed download after 10 min (x3 each time)
}
