"""Wave 1: fast metadata via TikTok's public oEmbed endpoint (caption, creator, thumbnail)."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from . import config, textutil

OEMBED = "https://www.tiktok.com/oembed"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


class Dead(Exception):
    """The video is gone (deleted/private/region-locked)."""


class Transient(Exception):
    """Network problem or rate limiting; retry later."""


class RateLimiter:
    def __init__(self, interval: float):
        self.interval = interval
        self.last = 0.0

    def wait(self) -> None:
        d = self.last + self.interval - time.monotonic()
        if d > 0:
            time.sleep(d)
        self.last = time.monotonic()


def canonical_url(video_id: str, url: str | None) -> str:
    if url and "/video/" in url and "tiktok.com/@" in url:
        return url.split("?")[0]
    return f"https://www.tiktok.com/@/video/{video_id}"


def fetch_oembed(url: str, client: httpx.Client) -> dict:
    try:
        r = client.get(OEMBED, params={"url": url}, timeout=20)
    except httpx.HTTPError as e:
        raise Transient(str(e)) from e
    if r.status_code in (429, 500, 502, 503, 504):
        raise Transient(f"HTTP {r.status_code}")
    if r.status_code in (400, 403, 404):
        raise Dead(f"oEmbed HTTP {r.status_code}")
    if r.status_code != 200:
        raise Transient(f"HTTP {r.status_code}")
    try:
        j = r.json()
    except ValueError as e:
        raise Transient("bad json") from e
    if j.get("code") or j.get("status_msg") and not j.get("title") and not j.get("author_name"):
        raise Dead(j.get("message") or j.get("status_msg") or "unavailable")
    return j


def oembed_to_fields(j: dict) -> dict:
    title = j.get("title") or ""
    handle = j.get("author_unique_id") or ""
    if not handle:
        au = j.get("author_url") or ""
        if "/@" in au:
            handle = au.split("/@")[-1].strip("/")
    return {
        "caption": title,
        "creator": handle.lower() or None,
        "creator_name": j.get("author_name"),
        "hashtags": textutil.hashtags(title),
        "_thumbnail_url": j.get("thumbnail_url"),
    }


def download_thumb(url: str, video_id: str, client: httpx.Client) -> str | None:
    if not url:
        return None
    try:
        r = client.get(url, timeout=20, follow_redirects=True)
        if r.status_code != 200 or not r.content:
            return None
    except httpx.HTTPError:
        return None
    d = config.media_dir() / video_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "thumb.jpg"
    _save_resized(r.content, p)
    return str(p.relative_to(config.media_dir()))


def _save_resized(data: bytes, out: Path, width: int = 360) -> None:
    from PIL import Image
    import io
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.width > width:
            im = im.resize((width, int(im.height * width / im.width)))
        im.save(out, "JPEG", quality=82)
    except Exception:
        out.write_bytes(data)


def resolve_short_link(url: str) -> str | None:
    """vm.tiktok.com/xyz -> full URL (contains the numeric id). Needs network."""
    try:
        with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=15) as c:
            r = c.head(url)
            return str(r.url)
    except httpx.HTTPError:
        return None


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA}, follow_redirects=True)
