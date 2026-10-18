"""Parse TikTok data exports (JSON / ZIP / TXT) into (video_id, url, saved_at) records.

TikTok's export format drifts between versions: favourites have appeared under
  Activity -> Favorite Videos -> FavoriteVideoList
  Your Activity -> Favorite Videos -> FavoriteVideoList
  Activity -> Favorite Videos -> FavoriteVideoList with items {Date, Link} or {date, link}/{Date, VideoLink}
and the TXT export uses blocks of "Date: ...\nLink: ..." lines.
We therefore search the whole JSON tree for any list under a key that looks like
"favorite video(s) list" and fall back to scanning every {date, link}-like object.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from dateutil import parser as dtparser

VIDEO_ID_RE = re.compile(r"(?:/video/|/v/|item_id=|/share/video/|video_id=)(\d{8,25})")
SHORT_LINK_RE = re.compile(r"https?://(?:vm|vt|www)\.tiktok(?:v)?\.com/(?:t/)?[A-Za-z0-9]+/?$")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


@dataclass
class Save:
    video_id: str | None
    url: str
    saved_at: float | None


def _norm(k: str) -> str:
    return re.sub(r"[^a-z]", "", k.lower())


def video_id_from_url(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def parse_date(s: Any) -> float | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        return v / 1000 if v > 1e12 else v
    try:
        d = dtparser.parse(str(s))
    except (ValueError, OverflowError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)  # TikTok exports are UTC
    return d.timestamp()


def _pick(d: dict, *cands: str) -> Any:
    norm = {_norm(k): v for k, v in d.items()}
    for c in cands:
        if c in norm and norm[c] not in (None, ""):
            return norm[c]
    return None


def _item_to_save(item: Any) -> Save | None:
    if isinstance(item, str):
        vid = video_id_from_url(item)
        return Save(vid, item.strip(), None) if (vid or SHORT_LINK_RE.match(item.strip())) else None
    if not isinstance(item, dict):
        return None
    link = _pick(item, "link", "videolink", "url", "videourl", "sharelink", "video")
    date = _pick(item, "date", "favoritedate", "savedate", "createtime", "time", "timestamp", "datetime")
    if not isinstance(link, str):
        return None
    link = link.strip()
    vid = video_id_from_url(link)
    if not vid and not SHORT_LINK_RE.match(link):
        return None
    return Save(vid, link, parse_date(date))


FAV_LIST_KEYS = {"favoritevideolist", "favouritevideolist", "favoritevideoslist", "favoritevideo",
                 "favoritevideos", "favouritevideos", "favoriteslist", "favoritevideoitems", "savedvideolist"}


def _walk_fav_lists(node: Any, under_fav: bool = False) -> Iterable[list]:
    if isinstance(node, dict):
        for k, v in node.items():
            nk = _norm(k)
            fav = under_fav or ("favorite" in nk or "favourite" in nk) and "video" in nk
            if isinstance(v, list) and (nk in FAV_LIST_KEYS or fav):
                yield v
            else:
                yield from _walk_fav_lists(v, fav)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_fav_lists(v, under_fav)


NON_FAV_HINTS = ("history", "browsing", "like", "share", "comment", "search", "login", "watch", "post", "follow")


def _walk_any_links(node: Any) -> Iterable[Save]:
    if isinstance(node, dict):
        s = _item_to_save(node)
        if s:
            yield s
            return
        for k, v in node.items():
            if any(h in _norm(k) for h in NON_FAV_HINTS):
                continue  # never import watch history / likes / shares as "saves"
            yield from _walk_any_links(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_any_links(v)


def parse_json_obj(obj: Any) -> list[Save]:
    saves: list[Save] = []
    for lst in _walk_fav_lists(obj):
        for it in lst:
            s = _item_to_save(it)
            if s:
                saves.append(s)
    if not saves:
        # Unknown layout: accept anything that looks like a {date, link} favourite record
        # but skip obviously unrelated sections (watch history, likes) only if fav lists exist.
        saves = list(_walk_any_links(obj))
    return dedupe(saves)


def parse_txt(text: str) -> list[Save]:
    """TXT export: blocks like 'Date: 2024-03-01 12:00:00\\nLink: https://...'; also plain URL lists."""
    saves: list[Save] = []
    pending_date: float | None = None
    in_fav = None  # None = no section headers seen
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if re.match(r"^[=\-#\s]*[a-z ]+[=\-#\s]*$", low) and "http" not in low and ":" not in low:
            # section header such as "Favorite Videos:" or "Watch History"
            in_fav = ("favorite" in low or "favourite" in low)
            continue
        if low.endswith(":") and "http" not in low:
            in_fav = ("favorite" in low or "favourite" in low)
            continue
        m = re.match(r"^(date|time|favorite date)\s*[:=]\s*(.+)$", line, re.I)
        if m:
            pending_date = parse_date(m.group(2))
            continue
        for url in URL_RE.findall(line):
            if in_fav is False:
                continue
            vid = video_id_from_url(url)
            if vid or SHORT_LINK_RE.match(url):
                saves.append(Save(vid, url, pending_date))
                pending_date = None
    return dedupe(saves)


def parse_bytes(name: str, data: bytes) -> list[Save]:
    low = name.lower()
    if low.endswith(".zip") or data[:2] == b"PK":
        return parse_zip(data)
    text = data.decode("utf-8-sig", errors="replace")
    if low.endswith(".json") or text.lstrip()[:1] in "{[":
        try:
            return parse_json_obj(json.loads(text))
        except json.JSONDecodeError:
            pass
    return parse_txt(text)


def parse_zip(data: bytes) -> list[Save]:
    out: list[Save] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        # prefer files that look like the favourites / user data file
        def prio(n: str) -> int:
            l = n.lower()
            return 0 if "favorite" in l or "favourite" in l else 1 if "user_data" in l else 2
        for n in sorted(names, key=prio):
            l = n.lower()
            if n.endswith("/") or not (l.endswith(".json") or l.endswith(".txt")):
                continue
            if l.endswith(".txt") and prio(n) == 2 and any(x in l for x in ("browsing", "watch", "search", "comment", "login")):
                continue
            got = parse_bytes(n, z.read(n))
            out.extend(got)
            if got and l.endswith(".json"):
                break  # the JSON export holds everything
    return dedupe(out)


def dedupe(saves: list[Save]) -> list[Save]:
    seen: dict[str, Save] = {}
    for s in saves:
        key = s.video_id or s.url
        if key in seen:
            prev = seen[key]
            if s.saved_at and (prev.saved_at is None or s.saved_at < prev.saved_at):
                prev.saved_at = s.saved_at
            continue
        seen[key] = s
    return list(seen.values())


def fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
