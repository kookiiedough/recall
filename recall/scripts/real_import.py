"""Real network mini-import: export JSON -> oEmbed -> yt-dlp -> full deep index. Prints per-video results."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main(export_path: str, queries: list[str]) -> None:
    from recall import clusters, config, db, ingest, pipeline, search, stats
    from e2e_fixture import PeakRSS
    config.models_dir()
    con = db.connect()
    rep: dict = {"home": str(config.home())}
    with PeakRSS() as mon:
        rep["import"] = ingest.import_export_bytes(con, Path(export_path).name, Path(export_path).read_bytes())
        s = db.get_settings(con)
        prov = pipeline.TikTokProvider(float(s["oembed_rate"]))
        t = time.time()
        while pipeline.wave1_batch(con, prov, s, limit=50):
            pass
        rep["wave1_s"] = round(time.time() - t, 1)
        t = time.time()
        while pipeline.wave2_batch(con, prov, s):
            pass
        rep["wave2_s"] = round(time.time() - t, 1)
        rep["peak_rss_mb"] = round(mon.peak / 2**20)
    rep["clusters"] = clusters.recluster(con)
    rows = []
    for r in con.execute("SELECT id, status, creator, duration, sound, error, signals, caption FROM videos ORDER BY saved_at DESC"):
        tm = con.execute("SELECT stage, round(seconds,2) FROM proc_stats WHERE video_id=?", (r["id"],)).fetchall()
        nch = dict(con.execute("SELECT kind, count(*) FROM chunks WHERE video_id=? GROUP BY kind", (r["id"],)).fetchall())
        rows.append({"id": r["id"], "status": r["status"], "creator": r["creator"], "duration": r["duration"], "sound": r["sound"],
                     "error": r["error"], "chunks": nch, "timings": dict(tm), "caption": (r["caption"] or "")[:70]})
    rep["videos"] = rows
    rep["queries"] = []
    for q in queries:
        res = search.search(q, con=con)
        rep["queries"].append({"q": q, "n": res["total"], "ms": res["timing"]["total_ms"],
                               "top": [(x["creator"], x["best_t"], [f"{w['label']}:{w['score']}" for w in x["why"][:3]]) for x in res["results"][:3]]})
    rep["stats"] = stats.overview(con)
    out = ROOT / "tests" / "real_import_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
