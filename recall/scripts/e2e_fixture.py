"""End-to-end run on the synthetic fixture: import export JSON -> wave 1 -> wave 2 (all signals) ->
clusters -> targeted queries. Reports rank-1 accuracy per signal, chips, timestamps, latency, RAM."""
from __future__ import annotations

import json
import os
import resource
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "fixtures" / "out"


class PeakRSS:
    def __init__(self):
        self.peak = 0; self.proc = psutil.Process(); self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self.proc.memory_info().rss)
            time.sleep(0.05)

    def __enter__(self):
        self.t.start(); return self

    def __exit__(self, *a):
        self._stop.set(); self.t.join()


def main() -> int:
    home = Path(os.environ.get("RECALL_HOME") or tempfile.mkdtemp(prefix="recall-e2e-"))
    os.environ["RECALL_HOME"] = str(home)
    os.environ.setdefault("RECALL_MODELS", str(ROOT / "data" / "models"))
    if not (FIX / "meta.json").exists():
        sys.exit("run fixtures/make_fixture.py first")
    from recall import clusters, config, db, ingest, pipeline, search, stats
    from recall.fixture_provider import FixtureProvider
    config.models_dir()
    con = db.connect()
    report: dict = {"home": str(home)}
    with PeakRSS() as mon:
        t0 = time.time()
        r = ingest.import_export_bytes(con, "user_data.json", (FIX / "user_data.json").read_bytes())
        report["import"] = r
        # re-import must be idempotent
        r2 = ingest.import_export_bytes(con, "user_data.json", (FIX / "user_data.json").read_bytes())
        report["reimport_added"] = r2["added"]
        prov = FixtureProvider(FIX)
        s = db.get_settings(con)
        w1 = time.time()
        while pipeline.wave1_batch(con, prov, s, limit=50):
            pass
        report["wave1_s"] = round(time.time() - w1, 2)
        w2 = time.time()
        n = 0
        while True:
            k = pipeline.wave2_batch(con, prov, s)
            if not k:
                break
            n += k
        report["wave2_s"] = round(time.time() - w2, 2)
        report["deep_indexed"] = n
        report["per_video_wave2_s"] = round((time.time() - w2) / max(1, n), 2)
        report["ingest_peak_rss_mb"] = round(mon.peak / 2**20)
        report["clusters"] = clusters.recluster(con)
        report["cluster_names"] = [(c["name"], c["size"], c["keywords"][:4]) for c in clusters.list_clusters(con)]
        # targeted queries
        targets = json.loads((FIX / "targets.json").read_text())
        search.search("warm up", con=con, log=False)
        per = defaultdict(lambda: [0, 0])
        rows, lat = [], []
        for t in targets:
            res = search.search(t["query"], con=con)
            lat.append(res["timing"]["total_ms"])
            top = res["results"][0] if res["results"] else None
            ok = bool(top and top["id"] == t["expect"])
            per[t["signal"]][0] += ok; per[t["signal"]][1] += 1
            rank = next((i + 1 for i, x in enumerate(res["results"]) if x["id"] == t["expect"]), None)
            rows.append({"signal": t["signal"], "query": t["query"], "ok": ok, "rank": rank, "n": res["total"],
                         "best_t": top and top["best_t"], "ms": res["timing"]["total_ms"],
                         "chips": [f"{w['label']}:{w['score']}" + (f"@{w['t']:.1f}s" if w.get('t') is not None else "")
                                   for w in (top["why"] if top else [])][:4]})
        # a query that should return nothing
        empty = search.search("vintage mechanical watches from the 1940s in silver", con=con)
        report["no_results_query"] = {"total": empty["total"], "fuzzy": [(f["id"][-2:], f["score"], f["signal"]) for f in empty.get("fuzzy", [])][:3],
                                      "suggestions": empty.get("suggestions")}
        # degraded path: pretend TikTok blocks the download for the OCR/visual targets, so only the oEmbed
        # thumbnail (frame at 1 s in this fixture) can be indexed; check what thumbnail-only search still finds.
        from recall import db as _db, downloader
        deg_t = [t for t in targets if t["signal"] in ("ocr", "visual")]
        deg_ids = sorted({t["expect"] for t in deg_t})

        class Blocked(type(prov)):
            def fetch_video(self, vid, url, tmp):
                if vid in deg_ids:
                    raise downloader.DownloadError("blocked", "Unexpected response from webpage request")
                return super().fetch_video(vid, url, tmp)
        bprov = Blocked(FIX)
        for vid in deg_ids:
            _db.set_status(con, vid, "meta")
        while pipeline.wave2_batch(con, bprov, s, ids=deg_ids):
            if not con.execute(f"SELECT count(*) FROM videos WHERE status='meta' AND id IN ({','.join('?'*len(deg_ids))})",
                               deg_ids).fetchone()[0]:
                break
        drows = []
        for t in deg_t:
            res = search.search(t["query"], con=con, log=False)
            rank = next((i + 1 for i, x in enumerate(res["results"]) if x["id"] == t["expect"]), None)
            hit = next((x for x in res["results"] if x["id"] == t["expect"]), None)
            drows.append({"signal": t["signal"], "query": t["query"], "rank": rank, "status": hit and hit["status"],
                          "chips": [f"{w['label']}:{w['score']}" for w in (hit["why"] if hit else [])][:3]})
        report["degraded_thumbnail_only"] = {
            "videos": len(deg_ids),
            "statuses": dict(con.execute(f"SELECT status, count(*) FROM videos WHERE id IN ({','.join('?'*len(deg_ids))}) GROUP BY status",
                                         deg_ids).fetchall()),
            "rank1": f"{sum(r['rank'] == 1 for r in drows)}/{len(drows)}",
            "found_in_results": f"{sum(r['rank'] is not None for r in drows)}/{len(drows)}",
            "queries": drows}
        report["peak_rss_mb"] = round(mon.peak / 2**20)
    report["ru_maxrss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    report["accuracy"] = {k: f"{a}/{b}" for k, (a, b) in per.items()}
    report["rank1_total"] = f"{sum(a for a, _ in per.values())}/{sum(b for _, b in per.values())}"
    lat.sort()
    report["latency_ms"] = {"p50": lat[len(lat)//2], "max": lat[-1], "mean": round(sum(lat)/len(lat), 1)}
    report["stats"] = {k: v for k, v in stats.overview(con).items() if k in ("counts", "coverage", "per_stage", "disk")}
    report["queries"] = rows
    out = ROOT / "tests" / "e2e_report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    for r in rows:
        print(("PASS" if r["ok"] else "FAIL"), f"{r['signal']:10s} rank={r['rank']} n={r['n']} t={r['best_t']} {r['ms']}ms  {r['query']!r}  {r['chips']}")
    for r in report["degraded_thumbnail_only"]["queries"]:
        print("THUMB", f"{r['signal']:7s} rank={r['rank']} status={r['status']} {r['query']!r} {r['chips']}")
    print(json.dumps({k: v for k, v in report.items() if k not in ("queries", "stats", "degraded_thumbnail_only")}, indent=1, default=str))
    print(json.dumps(report["stats"], indent=1))
    return 0 if sum(a for a, _ in per.values()) == sum(b for _, b in per.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
