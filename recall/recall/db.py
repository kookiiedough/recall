"""SQLite storage: videos (with a status state machine), text chunks + FTS5, vectors, clusters."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,          -- TikTok video id (or local-<hash>)
    url           TEXT,
    source        TEXT NOT NULL DEFAULT 'tiktok',  -- tiktok | url | local
    local_path    TEXT,                       -- for local-file ingest (never deleted by us)
    saved_at      REAL,                       -- unix ts when the user saved it (from export)
    added_at      REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',  -- new | meta | partial | deep | dead | failed
    stage         TEXT,                       -- current wave-2 stage for progress display
    progress      REAL DEFAULT 0,
    attempts      INTEGER DEFAULT 0,
    error         TEXT,
    caption       TEXT,
    creator       TEXT,                       -- unique handle (no @)
    creator_name  TEXT,
    sound         TEXT,
    duration      REAL,
    uploaded_at   REAL,
    hashtags      TEXT DEFAULT '[]',          -- json list, lowercase, no '#'
    thumb         TEXT,                       -- relative path under media/
    frames        TEXT DEFAULT '[]',          -- json list of {t, path}
    attrs         TEXT DEFAULT '{}',          -- json {name: {value: bool, evidence: str}}
    signals       TEXT DEFAULT '{}',          -- json {transcript: bool, ocr: bool, visual: bool, ...}
    note          TEXT,
    cluster_id    INTEGER,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_creator ON videos(creator);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY,
    video_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,   -- caption | transcript | ocr | creator | sound | hashtag | note
    t_start   REAL,
    t_end     REAL,
    text      TEXT NOT NULL,
    meta      TEXT              -- json (e.g. word timestamps)
);
CREATE INDEX IF NOT EXISTS idx_chunks_video ON chunks(video_id);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    text, kind UNINDEXED, video_id UNINDEXED,
    content='chunks', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO fts(rowid, text, kind, video_id) VALUES (new.id, new.text, new.kind, new.video_id);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO fts(fts, rowid, text, kind, video_id) VALUES ('delete', old.id, old.text, old.kind, old.video_id);
END;
CREATE VIRTUAL TABLE IF NOT EXISTS fts_vocab USING fts5vocab(fts, 'row');

CREATE TABLE IF NOT EXISTS vectors (
    id        INTEGER PRIMARY KEY,
    video_id  TEXT NOT NULL,
    space     TEXT NOT NULL,   -- 'text' (MiniLM, 384d) | 'image' (CLIP, 512d)
    chunk_id  INTEGER,         -- for text vectors
    t         REAL,            -- for image vectors: frame timestamp (NULL = thumbnail)
    vec       BLOB NOT NULL    -- float16 little-endian, L2-normalised
);
CREATE INDEX IF NOT EXISTS idx_vectors_video ON vectors(video_id);
CREATE INDEX IF NOT EXISTS idx_vectors_space ON vectors(space);

CREATE TABLE IF NOT EXISTS clusters (
    id          INTEGER PRIMARY KEY,
    auto_name   TEXT,
    custom_name TEXT,
    pinned      INTEGER DEFAULT 0,
    keywords    TEXT DEFAULT '[]',
    size        INTEGER DEFAULT 0,
    created_at  REAL
);

CREATE TABLE IF NOT EXISTS deleted (id TEXT PRIMARY KEY, at REAL);  -- tombstones: re-imports skip these
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS proc_stats (
    id INTEGER PRIMARY KEY, video_id TEXT, stage TEXT, seconds REAL, at REAL
);
CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY, query TEXT, ms REAL, n INTEGER, at REAL
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY, name TEXT, kind TEXT, found INTEGER, added INTEGER, at REAL
);
"""

_local = threading.local()
_version_lock = threading.Lock()
_data_version = [0]


def bump_version() -> None:
    """Called after any write that affects search (chunks/vectors/videos)."""
    with _version_lock:
        _data_version[0] += 1


def data_version() -> int:
    return _data_version[0]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or config.db_path()), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """v0.1 marked download failures 'failed' (unsearchable). They now go back to the deep queue, where a
    failing download ends as a searchable thumbnail-only 'partial' save instead."""
    where = "status='failed' AND error LIKE 'download:%' AND caption IS NOT NULL"
    try:
        if con.execute(f"SELECT 1 FROM videos WHERE {where} LIMIT 1").fetchone():
            con.execute(f"UPDATE videos SET status='meta', attempts=0, stage=NULL WHERE {where}")
            con.commit()
    except sqlite3.OperationalError:   # another connection is writing; try again next open
        con.rollback()


def get() -> sqlite3.Connection:
    """Per-thread connection to the configured DB."""
    key = str(config.db_path())
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "key", None) != key:
        con = connect()
        _local.con, _local.key = con, key
    return con


def reset_thread_connection() -> None:
    con = getattr(_local, "con", None)
    if con is not None:
        con.close()
    _local.con = None


# ---------------------------------------------------------------- state machine
STATUSES = ("new", "meta", "partial", "deep", "dead", "failed")
# partial = video download failed (or downloads are off): wave-1 metadata + thumbnail-only CLIP/OCR are indexed,
# the save stays searchable, `error` says why, and retriable failures are re-tried automatically later.
LIVE = ("meta", "partial", "deep")
LIVE_SQL = "('meta','partial','deep')"
TRANSITIONS: dict[str, set[str]] = {
    "new": {"meta", "dead", "failed"},
    "meta": {"deep", "partial", "failed", "dead", "meta"},   # meta->meta = metadata refresh
    "partial": {"deep", "partial", "meta", "new"},           # automatic / manual download retry
    "deep": {"meta", "deep"},                      # re-index (e.g. settings changed)
    "failed": {"new", "meta"},                     # manual / automatic retry
    "dead": {"new"},                               # manual retry of a dead link
}


class InvalidTransition(ValueError):
    pass


def set_status(con: sqlite3.Connection, video_id: str, new: str, **fields: Any) -> None:
    row = con.execute("SELECT status FROM videos WHERE id=?", (video_id,)).fetchone()
    if row is None:
        raise KeyError(video_id)
    old = row["status"]
    if new not in STATUSES:
        raise InvalidTransition(f"unknown status {new}")
    if new != old and new not in TRANSITIONS[old]:
        raise InvalidTransition(f"{old} -> {new} not allowed for {video_id}")
    fields["status"] = new
    fields["updated_at"] = time.time()
    update(con, video_id, **fields)


def _json_default(o):
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o).__name__)


def update(con: sqlite3.Connection, video_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if isinstance(v, (list, dict)):
            v = json.dumps(v, default=_json_default)
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(video_id)
    con.execute(f"UPDATE videos SET {', '.join(cols)} WHERE id=?", vals)
    con.commit()


def upsert_new_video(con: sqlite3.Connection, video_id: str, url: str | None, saved_at: float | None,
                     source: str = "tiktok", local_path: str | None = None) -> bool:
    """Idempotent insert keyed by video id. Returns True if it was new."""
    if con.execute("SELECT 1 FROM deleted WHERE id=?", (video_id,)).fetchone():
        return False
    cur = con.execute(
        "INSERT OR IGNORE INTO videos(id, url, source, local_path, saved_at, added_at, status, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (video_id, url, source, local_path, saved_at, time.time(), "new", time.time()),
    )
    if cur.rowcount == 0 and saved_at is not None:
        # keep the earliest known save date
        con.execute("UPDATE videos SET saved_at=? WHERE id=? AND (saved_at IS NULL OR saved_at>?)",
                    (saved_at, video_id, saved_at))
    con.commit()
    return cur.rowcount > 0


def replace_chunks(con: sqlite3.Connection, video_id: str, kinds: Iterable[str],
                   rows: Iterable[tuple[str, float | None, float | None, str, Any]]) -> list[int]:
    """Idempotently replace all chunks of the given kinds for a video. rows: (kind, t0, t1, text, meta)."""
    kinds = list(kinds)
    q = ",".join("?" * len(kinds))
    old_ids = [r[0] for r in con.execute(
        f"SELECT id FROM chunks WHERE video_id=? AND kind IN ({q})", (video_id, *kinds))]
    if old_ids:
        con.execute(f"DELETE FROM vectors WHERE space='text' AND chunk_id IN ({','.join('?'*len(old_ids))})", old_ids)
        con.execute(f"DELETE FROM chunks WHERE video_id=? AND kind IN ({q})", (video_id, *kinds))
    ids = []
    for kind, t0, t1, text, meta in rows:
        text = (text or "").strip()
        if not text:
            continue
        cur = con.execute("INSERT INTO chunks(video_id, kind, t_start, t_end, text, meta) VALUES (?,?,?,?,?,?)",
                          (video_id, kind, t0, t1, text, json.dumps(meta) if meta is not None else None))
        ids.append(cur.lastrowid)
    con.commit()
    bump_version()
    return ids


def delete_video(con: sqlite3.Connection, video_id: str, tombstone: bool = True) -> None:
    if tombstone:
        con.execute("INSERT OR REPLACE INTO deleted(id, at) VALUES (?,?)", (video_id, time.time()))
    con.execute("DELETE FROM vectors WHERE video_id=?", (video_id,))
    con.execute("DELETE FROM chunks WHERE video_id=?", (video_id,))
    con.execute("DELETE FROM videos WHERE id=?", (video_id,))
    con.commit()
    bump_version()


# ---------------------------------------------------------------- settings
def get_settings(con: sqlite3.Connection | None = None) -> dict[str, Any]:
    con = con or get()
    s = dict(config.DEFAULT_SETTINGS)
    for r in con.execute("SELECT key, value FROM settings"):
        try:
            s[r["key"]] = json.loads(r["value"])
        except Exception:
            pass
    return s


def set_settings(con: sqlite3.Connection, values: dict[str, Any]) -> dict[str, Any]:
    for k, v in values.items():
        if k in config.DEFAULT_SETTINGS:
            con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, json.dumps(v)))
    con.commit()
    return get_settings(con)


def row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in ("hashtags", "frames", "attrs", "signals"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = [] if k in ("hashtags", "frames") else {}
    return d
