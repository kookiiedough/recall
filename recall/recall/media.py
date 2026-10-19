"""ffmpeg / yt-dlp helpers. Videos are only ever stored temporarily."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def probe_duration(path: str) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                             capture_output=True, text=True, timeout=60).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


def extract_audio(video: str, out_wav: str) -> bool:
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000", out_wav],
                       capture_output=True, timeout=600)
    return r.returncode == 0 and Path(out_wav).exists() and Path(out_wav).stat().st_size > 1000


def extract_frames(video: str, out_dir: Path, interval: float, width: int = 360) -> list[tuple[float, Path]]:
    """One frame every `interval` seconds (first at interval/2 to avoid black intro frames)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob("s_*.jpg"):
        p.unlink()
    fps = 1.0 / interval
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{interval/2:.2f}", "-i", video,
                        "-vf", f"fps={fps},scale={width}:-2", "-q:v", "4", str(out_dir / "s_%05d.jpg")],
                       capture_output=True, timeout=900)
    if r.returncode != 0:
        return []
    frames = sorted(out_dir.glob("s_*.jpg"))
    return [(interval / 2 + i * interval, p) for i, p in enumerate(frames)]


def frame_at(video: str, t: float, out: Path, width: int = 360) -> bool:
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "4", str(out)], capture_output=True, timeout=120)
    return r.returncode == 0 and out.exists()


def read_wav(path: str):
    """16 kHz mono PCM wav -> float32 numpy (avoids PyAV version issues inside faster-whisper)."""
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
        width = w.getsampwidth()
    a = np.frombuffer(data, dtype=np.int16 if width == 2 else np.int32).astype(np.float32)
    return a / (32768.0 if width == 2 else 2147483648.0)
