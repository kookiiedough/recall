"""Offline provider used by the deterministic test fixture. It stands in ONLY for the two network
calls (oEmbed metadata and the yt-dlp download); everything after that (ffmpeg, Whisper, OCR, CLIP,
MiniLM, FTS, fusion) is the real pipeline. Enabled with RECALL_FIXTURE=<fixtures/out dir>."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import config, wave1


class FixtureProvider:
    def __init__(self, root: str | Path, fallback=None):
        self.root = Path(root)
        self.meta = json.loads((self.root / "meta.json").read_text())
        self.fallback = fallback  # real provider for ids that aren't part of the fixture

    def metadata(self, video_id: str, url: str | None) -> dict:
        m = self.meta.get(video_id)
        if not m and self.fallback:
            return self.fallback.metadata(video_id, url)
        if not m:
            raise wave1.Dead("oEmbed HTTP 404 (not in fixture)")
        d = config.media_dir() / video_id
        d.mkdir(parents=True, exist_ok=True)
        thumb = d / "thumb.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", m["video"], "-frames:v", "1",
                        "-vf", "scale=360:-2", str(thumb)], capture_output=True)
        from . import textutil
        return {"caption": m["caption"], "creator": m["creator"], "creator_name": m["creator_name"],
                "hashtags": textutil.hashtags(m["caption"]), "thumb": f"{video_id}/thumb.jpg"}

    def fetch_video(self, video_id: str, url: str | None, tmp: Path):
        if video_id not in self.meta and self.fallback:
            return self.fallback.fetch_video(video_id, url, tmp)
        m = self.meta[video_id]
        info = {"id": video_id, "description": m["caption"], "duration": m["duration"], "track": m["track"],
                "artist": m["artist"], "uploader": m["creator"]}
        return Path(m["video"]), info, False
