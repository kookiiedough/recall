"""Ingestion pipeline: wave 1 (oEmbed metadata) and wave 2 (deep index), resumable and idempotent.

Status machine (see db.TRANSITIONS):  new -> meta -> deep,  new -> dead,  * -> failed (after retries),
meta -> partial when the video can't be downloaded (thumbnail-only CLIP/OCR index; retried later).
Every step is keyed by video id and *replaces* that video's derived rows, so re-running is safe and an
interrupted batch simply resumes: videos that never reached `deep` are picked up again next time.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import attrs as attrs_mod
from . import config, db, downloader, media, models, textutil, wave1

log = logging.getLogger("recall.pipeline")
MAX_ATTEMPTS = 3


# ------------------------------------------------------------------ providers
class TikTokProvider:
    """Real network provider: oEmbed for wave 1, yt-dlp (via recall.downloader) for wave 2."""

    def __init__(self, rate: float = 1.0):
        self.rl = wave1.RateLimiter(rate)
        self.client = wave1.client()
        self.settings: dict = {}
        self.wait: Callable[[float], bool] = lambda s: (time.sleep(s), False)[1]

    def configure(self, settings: dict, wait: Callable[[float], bool]) -> None:
        self.settings, self.wait = settings, wait

    def metadata(self, video_id: str, url: str | None) -> dict:
        self.rl.wait()
        j = wave1.fetch_oembed(wave1.canonical_url(video_id, url), self.client)
        f = wave1.oembed_to_fields(j)
        f["thumb"] = wave1.download_thumb(f.pop("_thumbnail_url", None), video_id, self.client)
        return f

    def fetch_video(self, video_id: str, url: str | None, tmp: Path) -> tuple[Path | None, dict, bool]:
        self.rl.wait()
        path, meta, profile = downloader.download(wave1.canonical_url(video_id, url), tmp, self.settings, self.wait)
        meta["_profile"] = profile
        return path, meta, True


@dataclass
class Item:
    row: dict
    video: Path | None = None
    is_temp: bool = False
    work: Path | None = None
    duration: float | None = None
    samples: list[tuple[float, Path]] = field(default_factory=list)
    keyframes: list[tuple[float, Path]] = field(default_factory=list)
    wav: Path | None = None
    segments: list[dict] = field(default_factory=list)
    ocr: list[dict] = field(default_factory=list)
    ocr_lines: int = 0
    frame_labels: list[str] = field(default_factory=list)
    img_vecs: list[tuple[float | None, np.ndarray]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    failed: str | None = None
    degraded: tuple[str, str] | None = None   # (error kind, message): download failed -> thumbnail-only index

    @property
    def id(self) -> str:
        return self.row["id"]


# ------------------------------------------------------------------ helpers
def meta_chunk_rows(v: dict, settings: dict) -> list[tuple]:
    rows: list[tuple] = []
    if v.get("caption"):
        rows.append(("caption", None, None, v["caption"], None))
    who = " ".join(x for x in [v.get("creator") and "@" + v["creator"], v.get("creator_name")] if x)
    if who:
        rows.append(("creator", None, None, who, None))
    if v.get("sound") and settings.get("sound", True):
        rows.append(("sound", None, None, v["sound"], None))
    tags = v.get("hashtags") or []
    if isinstance(tags, str):
        tags = json.loads(tags or "[]")
    if tags:
        rows.append(("hashtag", None, None, " ".join("#" + t for t in tags), None))
    if v.get("note"):
        rows.append(("note", None, None, v["note"], None))
    return rows


META_KINDS = ("caption", "creator", "sound", "hashtag", "note")
EMBED_KINDS = ("caption", "transcript", "ocr", "note", "sound", "hashtag")


def write_meta_chunks(con, video_id: str, settings: dict) -> None:
    v = db.row_to_dict(con.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone())
    ids = db.replace_chunks(con, video_id, META_KINDS, meta_chunk_rows(v, settings))
    if settings.get("text_embeddings", True):
        embed_chunks(con, ids)


def embed_chunks(con, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    q = ",".join("?" * len(chunk_ids))
    rows = con.execute(f"SELECT id, video_id, kind, text FROM chunks WHERE id IN ({q}) AND kind IN "
                       f"({','.join('?'*len(EMBED_KINDS))})", (*chunk_ids, *EMBED_KINDS)).fetchall()
    if not rows:
        return
    vecs = models.embed_text([r["text"][:1000] for r in rows])
    con.executemany("INSERT INTO vectors(video_id, space, chunk_id, t, vec) VALUES (?,?,?,?,?)",
                    [(r["video_id"], "text", r["id"], None, v.astype(np.float16).tobytes()) for r, v in zip(rows, vecs)])
    con.commit()
    db.bump_version()


def stat(con, video_id: str, stage: str, seconds: float) -> None:
    con.execute("INSERT INTO proc_stats(video_id, stage, seconds, at) VALUES (?,?,?,?)",
                (video_id, stage, seconds, time.time()))


def dedupe_ocr(entries: list[tuple[float, str, int]]) -> list[dict]:
    """entries: (t, text, n_lines). Keep a text the first time it appears; drop near-repeats."""
    kept: list[dict] = []
    for t, text, n in entries:
        norm = re.sub(r"\s+", " ", text.lower()).strip()
        if len(norm) < 3:
            continue
        dup = None
        for k in kept:
            if difflib.SequenceMatcher(None, k["norm"], norm).ratio() > 0.8 or norm in k["norm"]:
                dup = k
                break
        if dup:
            dup["t_end"] = t
            if len(norm) > len(dup["norm"]) and dup["norm"] in norm:
                dup["norm"], dup["text"] = norm, text
            continue
        kept.append({"t": t, "t_end": t, "text": text, "norm": norm})
    return kept


# ------------------------------------------------------------------ wave 1
_wave1_pause = {"until": 0.0, "n": 0}   # oEmbed rate-limit backoff (HTTP 429/403): 30 s, 60 s, ... max 10 min


def wave1_batch(con, provider, settings: dict, limit: int = 20, stop: Callable[[], bool] = lambda: False) -> int:
    if time.time() < _wave1_pause["until"]:
        return 0
    rows = [db.row_to_dict(r) for r in con.execute(
        "SELECT * FROM videos WHERE status='new' ORDER BY saved_at DESC LIMIT ?", (limit,))]
    done = 0
    for v in rows:
        if stop():
            break
        vid = v["id"]
        t0 = time.time()
        db.update(con, vid, stage="metadata", progress=0.05)
        if v["source"] == "local":
            name = Path(v["local_path"] or vid).stem
            caption = re.sub(r"[_\-]+", " ", name)
            db.set_status(con, vid, "meta", caption=caption, creator=None, hashtags=textutil.hashtags(caption),
                          stage=None, progress=0.1, attrs=attrs_mod.text_only(caption))
        else:
            try:
                f = provider.metadata(vid, v["url"])
                tags = f.get("hashtags") or []
                db.set_status(con, vid, "meta", caption=f.get("caption"), creator=f.get("creator"),
                              creator_name=f.get("creator_name"), hashtags=tags, thumb=f.get("thumb"),
                              sound=f.get("sound") if settings.get("sound", True) else None,
                              duration=f.get("duration"), stage=None, progress=0.1, error=None,
                              attrs=attrs_mod.text_only(f.get("caption") or ""))
            except wave1.Dead as e:
                db.set_status(con, vid, "dead", error=str(e)[:300], stage=None)
                continue
            except Exception as e:  # transient
                if re.search(r"HTTP (429|403)", str(e)):
                    _wave1_pause["n"] += 1
                    pause = min(600.0, 30.0 * 2 ** (_wave1_pause["n"] - 1))
                    _wave1_pause["until"] = time.time() + pause
                    log.warning("oEmbed rate-limited (%s); pausing metadata fetches for %.0f s", e, pause)
                    db.update(con, vid, error=f"metadata: {e} (rate-limited, will retry)"[:300], stage=None)
                    break
                att = (v["attempts"] or 0) + 1
                if att >= MAX_ATTEMPTS + 2:
                    db.set_status(con, vid, "failed", attempts=att, error=f"metadata: {e}"[:300], stage=None)
                else:
                    db.update(con, vid, attempts=att, error=f"metadata: {e}"[:300], stage=None)
                continue
        _wave1_pause["n"] = 0
        write_meta_chunks(con, vid, settings)
        stat(con, vid, "wave1", time.time() - t0)
        con.commit()
        done += 1
    return done


# ------------------------------------------------------------------ wave 2
def _progress(con, it: Item, stage: str, p: float) -> None:
    db.update(con, it.id, stage=stage, progress=p)


def _fail(con, it: Item, msg: str, dead: bool = False) -> None:
    it.failed = msg
    att = (it.row.get("attempts") or 0) + 1
    if dead:
        db.set_status(con, it.id, "dead", error=msg[:300], stage=None, attempts=att)
    elif att >= MAX_ATTEMPTS:
        db.set_status(con, it.id, "failed", error=msg[:300], stage=None, attempts=att)
    else:
        db.update(con, it.id, error=msg[:300], stage=None, attempts=att, progress=0.1)


def _wait_fn(stop: Callable[[], bool]) -> Callable[[float], bool]:
    """Interruptible sleep: returns True if the worker was asked to stop while waiting."""
    def wait(seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if stop():
                return True
            time.sleep(min(0.5, max(0.0, end - time.time())))
        return stop()
    return wait


def _degrade(con, it: Item, kind: str, msg: str) -> None:
    """Download failed (or downloads are off): keep wave-1 metadata and index the oEmbed thumbnail only."""
    it.degraded = (kind, msg)
    th = it.row.get("thumb")
    thp = config.media_dir() / th if th else None
    if thp and thp.exists():
        it.samples = [(None, thp)]          # OCR the thumbnail; no timestamp (it's not a frame of known time)
    _progress(con, it, "thumbnail only · queued: OCR", 0.2)


def stage_fetch(con, provider, settings: dict, it: Item, breaker: downloader.Breaker | None = None) -> None:
    t0 = time.time()
    _progress(con, it, "downloading", 0.15)
    it.work = config.tmp_dir() / it.id
    it.work.mkdir(parents=True, exist_ok=True)
    if it.row["source"] != "local" and not settings.get("video_downloads", True):
        _degrade(con, it, "disabled", "video downloads are off in Settings")
        it.timings["fetch"] = time.time() - t0
        return
    try:
        if it.row["source"] == "local":
            it.video, meta, it.is_temp = Path(it.row["local_path"]), {}, False
            if not it.video.exists():
                raise FileNotFoundError(str(it.video))
        else:
            it.video, meta, it.is_temp = provider.fetch_video(it.id, it.row["url"], it.work)
        if not it.video or not Path(it.video).exists():
            raise downloader.DownloadError("no_file", "download produced no file")
    except wave1.Dead as e:
        _fail(con, it, f"download: {e}", dead=True)
        return
    except Exception as e:
        if it.row["source"] == "local":
            _fail(con, it, f"local file: {e}")
            return
        kind, msg = (e.kind, e.message) if isinstance(e, downloader.DownloadError) else \
            (downloader.classify(str(e)), downloader.concise(str(e)))
        log.warning("download failed %s [%s] %s", it.id, kind, msg)   # one concise line per video
        if breaker:
            breaker.failure(kind, msg, it.id)
        _degrade(con, it, kind, msg)
        it.timings["fetch"] = time.time() - t0
        return
    if breaker and it.row["source"] != "local":
        breaker.success()
    it.row["_profile"] = meta.get("_profile")
    fields: dict[str, Any] = {}
    desc = meta.get("description") or meta.get("title")
    if desc and len(desc) > len(it.row.get("caption") or ""):
        fields["caption"] = desc
    tags = list(dict.fromkeys((it.row.get("hashtags") or []) + textutil.hashtags(desc or "")
                              + [t.lower().lstrip("#") for t in (meta.get("tags") or []) if t]))
    fields["hashtags"] = tags
    if settings.get("sound", True) and (meta.get("track") or meta.get("artist")):
        fields["sound"] = " — ".join(x for x in [meta.get("track"), meta.get("artist")] if x)
    if meta.get("uploader_id") or meta.get("uploader"):
        if not it.row.get("creator"):
            fields["creator"] = (meta.get("uploader") or meta.get("uploader_id") or "").lower()
    if meta.get("timestamp"):
        fields["uploaded_at"] = float(meta["timestamp"])
    if not it.row.get("thumb") and meta.get("thumbnail") and getattr(provider, "client", None):
        try:
            th = wave1.download_thumb(meta["thumbnail"], it.id, provider.client)
            if th:
                fields["thumb"] = th
        except Exception:
            pass
    it.duration = media.probe_duration(str(it.video)) or meta.get("duration")
    fields["duration"] = it.duration
    db.update(con, it.id, **fields)
    it.row.update(fields)
    _progress(con, it, "sampling frames", 0.2)
    it.samples = media.extract_frames(str(it.video), it.work / "frames", float(settings.get("frame_interval", 2.0)))
    if settings.get("transcripts", True):
        wav = it.work / "audio.wav"
        it.wav = wav if media.extract_audio(str(it.video), str(wav)) else None
    # keyframes: evenly spaced subset of samples, copied to permanent media dir
    k = int(settings.get("max_keyframes", 8))
    if it.samples:
        idx = np.linspace(0, len(it.samples) - 1, num=min(k, len(it.samples))).round().astype(int)
        mdir = config.media_dir() / it.id
        mdir.mkdir(parents=True, exist_ok=True)
        for p in mdir.glob("k_*.jpg"):
            p.unlink()
        for i in dict.fromkeys(idx.tolist()):
            t, src = it.samples[i]
            dst = mdir / f"k_{int(t*1000):07d}.jpg"
            shutil.copy(src, dst)
            it.keyframes.append((t, dst))
        if not it.row.get("thumb"):
            shutil.copy(it.keyframes[0][1], mdir / "thumb.jpg")
            db.update(con, it.id, thumb=f"{it.id}/thumb.jpg")
            it.row["thumb"] = f"{it.id}/thumb.jpg"
    it.timings["fetch"] = time.time() - t0
    _progress(con, it, "queued: transcribe", 0.25)


def stage_transcribe(con, settings: dict, items: list[Item]) -> None:
    todo = [it for it in items if not it.failed and it.wav]
    if not todo or not settings.get("transcripts", True):
        return
    w = models.whisper(settings.get("whisper_model", "base"))
    for it in todo:
        t0 = time.time()
        _progress(con, it, "transcribing", 0.35)
        try:
            segs, _info = w.transcribe(media.read_wav(str(it.wav)), beam_size=1, word_timestamps=True, vad_filter=False,
                                       condition_on_previous_text=False)
            for s in segs:
                text = s.text.strip()
                if not text or (s.no_speech_prob > 0.7 and s.avg_logprob < -1.0):
                    continue
                words = [{"w": x.word.strip(), "t": round(x.start, 2)} for x in (s.words or [])]
                it.segments.append({"t0": round(s.start, 2), "t1": round(s.end, 2), "text": text, "words": words})
        except Exception as e:
            log.warning("transcribe failed for %s: %s", it.id, e)
        it.timings["transcribe"] = time.time() - t0
        _progress(con, it, "queued: OCR", 0.5)
    models.unload("whisper")  # free RAM before the next heavy model


def stage_ocr(con, settings: dict, items: list[Item]) -> None:
    todo = [it for it in items if not it.failed and it.samples]
    if not todo or not settings.get("ocr", True):
        return
    eng = models.ocr()
    for it in todo:
        t0 = time.time()
        _progress(con, it, ("thumbnail only · " if it.degraded else "") + "reading on-screen text", 0.6)
        raw: list[tuple[float, str, int]] = []
        for t, p in it.samples:
            try:
                r = eng(str(p))
                txts = [x for x, s in zip(r.txts or (), r.scores or ()) if s >= 0.6] if r and r.txts else []
            except Exception:
                txts = []
            it.ocr_lines += len(txts)
            if txts:
                raw.append((t, " ".join(txts), len(txts)))
        it.ocr = dedupe_ocr(raw)
        it.timings["ocr"] = time.time() - t0
        _progress(con, it, ("thumbnail only · " if it.degraded else "") + "queued: visual", 0.7)
    models.unload("ocr")


_label_vecs: np.ndarray | None = None


def stage_visual(con, settings: dict, items: list[Item]) -> None:
    global _label_vecs
    todo = [it for it in items if not it.failed and (it.keyframes or it.row.get("thumb"))]
    if not todo or not settings.get("visual", True):
        return
    if _label_vecs is None:
        _label_vecs = models.clip_text(list(attrs_mod.ZS_LABELS.values()))
    for it in todo:
        t0 = time.time()
        _progress(con, it, ("thumbnail only · " if it.degraded else "") + "visual embedding", 0.8)
        paths, ts = [], []
        for t, p in it.keyframes:
            paths.append(str(p)); ts.append(t)
        th = it.row.get("thumb")
        if th and (config.media_dir() / th).exists():
            paths.append(str(config.media_dir() / th)); ts.append(None)
        vecs = models.clip_images(paths)
        it.img_vecs = list(zip(ts, vecs))
        kf = np.array([v for t, v in it.img_vecs if t is not None]) if it.keyframes else \
            np.array([v for _, v in it.img_vecs]) if it.img_vecs else np.zeros((0, 512))
        it.frame_labels = attrs_mod.zero_shot(kf, _label_vecs) if len(kf) else []
        it.timings["visual"] = time.time() - t0


def finalize(con, settings: dict, it: Item) -> None:
    t0 = time.time()
    _progress(con, it, ("thumbnail only · " if it.degraded else "") + "indexing", 0.9)
    vid = it.id
    rows = [("transcript", s["t0"], s["t1"], s["text"], {"words": s["words"]}) for s in it.segments]
    rows += [("ocr", o["t"], o["t_end"], o["text"], None) for o in it.ocr]
    ids = db.replace_chunks(con, vid, ("transcript", "ocr"), rows)
    write_meta_chunks(con, vid, settings)  # refresh caption/sound/hashtags from yt-dlp metadata
    if settings.get("text_embeddings", True):
        embed_chunks(con, ids)
    con.execute("DELETE FROM vectors WHERE video_id=? AND space='image'", (vid,))
    con.executemany("INSERT INTO vectors(video_id, space, chunk_id, t, vec) VALUES (?,?,?,?,?)",
                    [(vid, "image", None, t, v.astype(np.float16).tobytes()) for t, v in it.img_vecs])
    v = db.row_to_dict(con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())
    full_text = " ".join([v.get("caption") or ""] + [s["text"] for s in it.segments] + [o["text"] for o in it.ocr])
    speech = sum(max(0.0, s["t1"] - s["t0"]) for s in it.segments)
    a = attrs_mod.compute(full_text, it.duration, speech, it.frame_labels,
                          it.ocr_lines / max(1, len(it.samples)))
    signals = {
        "transcript": bool(it.segments) if settings.get("transcripts", True) else None,
        "ocr": bool(it.ocr) if settings.get("ocr", True) else None,
        "visual": bool(it.img_vecs) if settings.get("visual", True) else None,
        "text_embeddings": bool(settings.get("text_embeddings", True)),
        "sound": bool(v.get("sound")) if settings.get("sound", True) else None,
        "frame_labels": it.frame_labels,
    }
    if it.row.get("_profile"):
        signals["download_profile"] = it.row["_profile"]
    frames = [{"t": round(t, 2), "path": str(p.relative_to(config.media_dir()))} for t, p in it.keyframes]
    if it.degraded:
        kind, msg = it.degraded
        att = (it.row.get("attempts") or 0) + (0 if kind == "disabled" else 1)
        signals.update(degraded=True, download_error=kind, transcript=None if signals["transcript"] is None else False,
                       download_retry=1 if (kind == "disabled" or (downloader.KIND_RETRY.get(kind, True)
                                                                   and att < MAX_ATTEMPTS)) else 0)
        reason = "video downloads are off — thumbnail-only index" if kind == "disabled" else \
            f"download failed [{kind}]: {msg} — thumbnail-only index"
        db.set_status(con, vid, "partial", attrs=a, signals=signals, frames=[], stage=None, progress=0.6,
                      error=reason[:300], attempts=att)
    else:
        db.set_status(con, vid, "deep", attrs=a, signals=signals, frames=frames, stage=None, progress=1.0,
                      error=None, attempts=0)
    it.timings["index"] = time.time() - t0
    for k, s in it.timings.items():
        stat(con, vid, k, s)
    stat(con, vid, "wave2_total", sum(it.timings.values()))
    con.commit()
    db.bump_version()


def cleanup(it: Item) -> None:
    """Delete the downloaded video, audio and non-key frames. Keyframes + thumbnail stay in media/."""
    if it.work and it.work.exists():
        shutil.rmtree(it.work, ignore_errors=True)


def _retry_due(con, settings: dict, limit: int) -> list:
    """partial videos whose download should be retried now (exponential delay: d, 3d, 9d ...)."""
    if not settings.get("video_downloads", True):
        return []
    base = float(settings.get("download_retry_delay_s", 600))
    rows = con.execute("SELECT * FROM videos WHERE status='partial' AND source!='local' AND "
                       "json_extract(signals, '$.download_retry')=1 ORDER BY updated_at LIMIT 200").fetchall()
    now = time.time()
    due = [r for r in rows if r["attempts"] == 0 or now - (r["updated_at"] or 0) >= base * 3 ** max(0, r["attempts"] - 1)]
    return due[:limit]


def _mark_waiting(con, items: list[Item], label: str, p: float) -> None:
    for it in items:
        if not it.failed:
            _progress(con, it, ("thumbnail only · " if it.degraded else "") + label, p)


def wave2_batch(con, provider, settings: dict, batch_size: int | None = None,
                stop: Callable[[], bool] = lambda: False, ids: list[str] | None = None,
                breaker: downloader.Breaker | None = None) -> int:
    n = int(batch_size or settings.get("batch_size", 4))
    if ids:
        q = ",".join("?" * len(ids))
        rows = con.execute(f"SELECT * FROM videos WHERE status IN ('meta','partial') AND id IN ({q}) LIMIT ?",
                           (*ids, n)).fetchall()
    else:
        rows = con.execute("SELECT * FROM videos WHERE status='meta' ORDER BY saved_at DESC LIMIT ?", (n,)).fetchall()
        if len(rows) < n and not (breaker and breaker.is_open()):
            rows = list(rows) + _retry_due(con, settings, n - len(rows))
    items = [Item(db.row_to_dict(r)) for r in rows]
    if not items:
        return 0
    wait = _wait_fn(stop)
    if hasattr(provider, "configure"):
        provider.configure(settings, wait)
    _mark_waiting(con, items, "queued: download", 0.12)
    try:
        fetched: list[Item] = []
        for it in items:
            if stop():
                return 0
            if breaker and it.row["source"] != "local" and settings.get("video_downloads", True):
                if breaker.is_open():
                    break                    # circuit open: leave the rest queued, UI shows a banner
                if wait(breaker.backoff_s()):  # exponential backoff while failures pile up
                    return 0
            stage_fetch(con, provider, settings, it, breaker)
            fetched.append(it)
        for it in items:
            if it not in fetched:
                db.update(con, it.id, stage=None)
        items = fetched
        # models run sequentially over the whole batch: whisper -> OCR -> CLIP -> MiniLM
        stage_transcribe(con, settings, items)
        if stop():
            return 0
        _mark_waiting(con, [i for i in items if not i.wav or not settings.get("transcripts", True)],
                      "queued: OCR", 0.5)
        stage_ocr(con, settings, items)
        if stop():
            return 0
        _mark_waiting(con, [i for i in items if not i.samples or not settings.get("ocr", True)],
                      "queued: visual", 0.7)
        stage_visual(con, settings, items)
        done = 0
        for it in items:
            if not it.failed:
                finalize(con, settings, it)
                done += 1
        return done
    finally:
        for it in items:
            cleanup(it)
        for it in items:  # interrupted items fall back to their queue state and are retried later
            r = con.execute("SELECT status, stage FROM videos WHERE id=?", (it.id,)).fetchone()
            if r and r["status"] in ("meta", "partial") and r["stage"] is not None:
                db.update(con, it.id, stage=None)
