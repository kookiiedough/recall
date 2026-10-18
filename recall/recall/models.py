"""Lazy model loading with a strict budget: at most ONE heavy *ingest-only* model (Whisper, OCR)
is resident at a time. The two small query encoders (MiniLM text, CLIP) are shared by search and
ingest and stay loaded once used. Everything runs on CPU."""
from __future__ import annotations

import gc
import os
import threading
from typing import Any

import numpy as np

from . import config

_lock = threading.RLock()
_models: dict[str, Any] = {}
INGEST_ONLY = {"whisper", "ocr"}

THREADS = max(1, min(4, (os.cpu_count() or 2)))


def _torch():
    import torch
    torch.set_num_threads(THREADS)
    return torch


def _evict_ingest_except(name: str) -> None:
    for k in list(_models):
        if k.split(":")[0] in INGEST_ONLY and k != name:
            del _models[k]
    gc.collect()


def unload(prefix: str) -> None:
    with _lock:
        for k in list(_models):
            if k.split(":")[0] == prefix:
                del _models[k]
        gc.collect()


def loaded() -> list[str]:
    return list(_models)


def whisper(size: str = "base"):
    key = f"whisper:{size}"
    with _lock:
        if key not in _models:
            _evict_ingest_except(key)
            from faster_whisper import WhisperModel
            _models[key] = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=THREADS,
                                        download_root=str(config.models_dir() / "whisper"))
        return _models[key]


def ocr():
    key = "ocr"
    with _lock:
        if key not in _models:
            _evict_ingest_except(key)
            from rapidocr import RapidOCR
            _models[key] = RapidOCR(params={"Global.log_level": "error"})
        return _models[key]


def minilm():
    key = "minilm"
    with _lock:
        if key not in _models:
            config.models_dir()
            _torch()
            from sentence_transformers import SentenceTransformer
            _models[key] = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
        return _models[key]


def clip():
    key = "clip"
    with _lock:
        if key not in _models:
            config.models_dir()
            _torch()
            import open_clip
            model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
            model.eval()
            tok = open_clip.get_tokenizer("ViT-B-32")
            _models[key] = (model, preprocess, tok)
        return _models[key]


def embed_text(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    m = minilm()
    v = m.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(v, dtype=np.float32)


def clip_text(texts: list[str]) -> np.ndarray:
    torch = _torch()
    model, _, tok = clip()
    with torch.no_grad():
        f = model.encode_text(tok(texts))
        f = f / f.norm(dim=-1, keepdim=True)
    return f.cpu().numpy().astype(np.float32)


def clip_images(paths: list[str]) -> np.ndarray:
    if not paths:
        return np.zeros((0, 512), dtype=np.float32)
    torch = _torch()
    from PIL import Image
    model, pre, _ = clip()
    out = []
    with torch.no_grad():
        for i in range(0, len(paths), 8):
            batch = torch.stack([pre(Image.open(p).convert("RGB")) for p in paths[i:i + 8]])
            f = model.encode_image(batch)
            f = f / f.norm(dim=-1, keepdim=True)
            out.append(f.cpu().numpy())
    return np.concatenate(out).astype(np.float32)
