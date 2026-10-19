"""Real index statistics for the UI (no fake telemetry)."""
from __future__ import annotations

import os
import shutil

from . import config, db, downloader


def dir_size(p) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def overview(con=None) -> dict:
    con = con or db.get()
    counts = {s: 0 for s in db.STATUSES}
    counts.update(dict(con.execute("SELECT status, count(*) FROM videos GROUP BY status").fetchall()))
    live = counts["meta"] + counts["partial"] + counts["deep"]
    def cov(kind: str) -> int:
        return con.execute("SELECT count(DISTINCT c.video_id) FROM chunks c JOIN videos v ON v.id=c.video_id "
                           "WHERE c.kind=? AND v.status IN ('meta','partial','deep')", (kind,)).fetchone()[0]
    vis = con.execute("SELECT count(DISTINCT x.video_id) FROM vectors x JOIN videos v ON v.id=x.video_id "
                      "WHERE x.space='image' AND v.status IN ('meta','partial','deep')").fetchone()[0]
    coverage = {}
    for name, n in (("Captions", cov("caption")), ("Transcripts", cov("transcript")), ("On-screen text", cov("ocr")),
                    ("Visual", vis), ("Sound", cov("sound")), ("Hashtags", cov("hashtag"))):
        coverage[name] = {"videos": n, "pct": round(100 * n / live) if live else 0}
    home = config.home()
    dbsize = sum(os.path.getsize(p) for p in [config.db_path(), str(config.db_path()) + "-wal"] if os.path.exists(p))
    media = dir_size(config.media_dir())
    disk = shutil.disk_usage(home)
    lat = con.execute("SELECT avg(ms), count(*) FROM (SELECT ms FROM search_log ORDER BY id DESC LIMIT 50)").fetchone()
    last = con.execute("SELECT ms FROM search_log ORDER BY id DESC LIMIT 1").fetchone()
    per_stage = {r[0]: {"avg_s": round(r[1], 2), "n": r[2]} for r in con.execute(
        "SELECT stage, avg(seconds), count(*) FROM proc_stats GROUP BY stage")}
    return {
        "counts": counts, "live": live, "total": sum(counts.values()),
        "deep_pct": round(100 * counts["deep"] / live) if live else 0,
        "coverage": coverage,
        "disk": {"db_bytes": dbsize, "media_bytes": media, "total_bytes": dbsize + media,
                 "disk_free": disk.free, "disk_total": disk.total, "home": str(home)},
        "latency": {"avg_ms": round(lat[0], 1) if lat[0] else None, "n": lat[1], "last_ms": round(last[0], 1) if last else None},
        "per_stage": per_stage,
        "clusters": con.execute("SELECT count(*) FROM clusters WHERE size > 0").fetchone()[0],
        "partial_reasons": dict(con.execute(
            "SELECT coalesce(json_extract(signals, '$.download_error'), 'other'), count(*) FROM videos "
            "WHERE status='partial' GROUP BY 1").fetchall()),
        "download_breaker": downloader.Breaker(con).public(),
        "downloader": downloader.environment(),
    }
