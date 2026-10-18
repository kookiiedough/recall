"""Entry points that add saves to the index (export file, pasted links, local video files)."""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

from . import config, db, importer, wave1

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def add_saves(con, saves: list[importer.Save], name: str, kind: str, resolve_short: bool = True) -> dict:
    added = 0
    skipped = 0
    for s in saves:
        vid = s.video_id
        url = s.url
        if not vid and resolve_short:
            full = wave1.resolve_short_link(url)
            vid = importer.video_id_from_url(full or "")
            if full and vid:
                url = full
        if not vid:
            skipped += 1
            continue
        if db.upsert_new_video(con, vid, url, s.saved_at, source="tiktok" if kind != "url" else "url"):
            added += 1
    con.execute("INSERT INTO imports(name, kind, found, added, at) VALUES (?,?,?,?,?)",
                (name, kind, len(saves), added, time.time()))
    con.commit()
    db.bump_version()
    return {"found": len(saves), "added": added, "already_indexed": len(saves) - added - skipped,
            "unresolved": skipped}


def import_export_bytes(con, name: str, data: bytes) -> dict:
    saves = importer.parse_bytes(name, data)
    return add_saves(con, saves, name, "export")


def import_links(con, text: str) -> dict:
    urls = importer.URL_RE.findall(text or "")
    saves = [importer.Save(importer.video_id_from_url(u), u.strip(), time.time()) for u in urls]
    return add_saves(con, importer.dedupe(saves), "pasted links", "url")


def file_id(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    h.update(str(path.stat().st_size).encode())
    return "local-" + h.hexdigest()[:16]


def import_local_file(con, src: Path, keep_copy: bool = False) -> dict:
    """Index a local MP4/MOV. Uploaded files are copied into RECALL_HOME/uploads (deleted after indexing
    unless keep_copy); a path on disk is indexed in place and never deleted."""
    if src.suffix.lower() not in VIDEO_EXT:
        return {"found": 1, "added": 0, "error": f"unsupported file type {src.suffix}"}
    vid = file_id(src)
    added = db.upsert_new_video(con, vid, None, time.time(), source="local", local_path=str(src))
    con.execute("INSERT INTO imports(name, kind, found, added, at) VALUES (?,?,?,?,?)",
                (src.name, "local", 1, int(added), time.time()))
    con.commit()
    return {"found": 1, "added": int(added), "id": vid}


def save_upload(name: str, data: bytes) -> Path:
    d = config.home() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", Path(name).name)
    p = d / safe
    p.write_bytes(data)
    return p
