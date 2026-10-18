"""FastAPI app: JSON API + the single-page UI. Binds to localhost only by default."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, clusters, config, db, export, ingest, models, search, stats
from .worker import Worker

STATIC = Path(__file__).parent / "static"


def create_app(worker: Worker | None = None, start_worker: bool = True, warm: bool = True) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _startup()
        yield
        app.state.worker.stop()

    app = FastAPI(title="Recall", version=__version__, lifespan=lifespan)
    app.state.worker = worker or Worker()

    def _startup():
        if start_worker:
            app.state.worker.start()
        if warm:
            import threading
            def _warm():
                try:
                    s = db.get_settings()
                    if s.get("text_embeddings", True):
                        models.embed_text(["warm up"])
                    if s.get("visual", True):
                        models.clip_text(["warm up"])
                except Exception:
                    pass
            threading.Thread(target=_warm, daemon=True).start()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/media/{path:path}")
    def media(path: str):
        base = config.media_dir().resolve()
        p = (base / path).resolve()
        if base not in p.parents or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------ search
    @app.get("/api/search")
    def api_search(q: str = "", f: str = "{}", limit: int = 60, offset: int = 0):
        try:
            facets = {k: v if isinstance(v, list) else [v] for k, v in json.loads(f or "{}").items() if v}
        except ValueError:
            facets = {}
        return search.search(q, facets=facets, limit=min(limit, 200), offset=offset)

    @app.get("/api/facets")
    def api_facets():
        return search.facet_counts(db.get(), None)

    @app.get("/api/stats")
    def api_stats():
        s = stats.overview()
        w = app.state.worker
        s["worker"] = {"state": w.state, "error": w.last_error, "models_loaded": models.loaded()}
        s["version"] = __version__
        return s

    @app.get("/api/queue")
    def api_queue(limit: int = 40):
        con = db.get()
        active = con.execute("SELECT id, url, source, local_path, status, stage, progress, error, caption, creator, duration, attempts "
                             "FROM videos WHERE stage IS NOT NULL ORDER BY updated_at DESC LIMIT 10").fetchall()
        pending = con.execute("SELECT id, url, source, local_path, status, stage, progress, error, caption, creator, duration, attempts "
                              "FROM videos WHERE status IN ('new','meta') AND stage IS NULL ORDER BY saved_at DESC LIMIT ?", (limit,)).fetchall()
        recent = con.execute("SELECT id, url, source, local_path, status, stage, progress, error, caption, creator, duration, attempts "
                             "FROM videos WHERE status IN ('deep','dead','failed') ORDER BY updated_at DESC LIMIT 15").fetchall()
        counts = dict(con.execute("SELECT status, count(*) FROM videos GROUP BY status").fetchall())
        imports = [dict(r) for r in con.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 5")]
        s = db.get_settings(con)
        return {"active": [dict(r) for r in active], "pending": [dict(r) for r in pending], "recent": [dict(r) for r in recent],
                "counts": counts, "imports": imports, "worker": app.state.worker.state, "paused": bool(s.get("worker_paused")),
                "deep_index": bool(s.get("deep_index", True))}

    # ------------------------------------------------------------ import
    @app.post("/api/import/file")
    async def api_import_file(files: list[UploadFile] = File(...)):
        con = db.get()
        results = []
        for uf in files:
            data = await uf.read()
            name = uf.filename or "upload"
            if Path(name).suffix.lower() in ingest.VIDEO_EXT:
                p = ingest.save_upload(name, data)
                results.append({"name": name, **ingest.import_local_file(con, p)})
            else:
                try:
                    results.append({"name": name, **ingest.import_export_bytes(con, name, data)})
                except Exception as e:
                    results.append({"name": name, "error": f"could not parse: {e}"})
        app.state.worker.kick()
        return {"results": results}

    @app.post("/api/import/links")
    def api_import_links(body: dict = Body(...)):
        r = ingest.import_links(db.get(), body.get("text", ""))
        app.state.worker.kick()
        return r

    @app.post("/api/import/path")
    def api_import_path(body: dict = Body(...)):
        p = Path(body.get("path", "")).expanduser()
        if not p.exists():
            raise HTTPException(400, "path not found")
        con = db.get()
        files = [p] if p.is_file() else [x for x in sorted(p.rglob("*")) if x.suffix.lower() in ingest.VIDEO_EXT]
        res = [ingest.import_local_file(con, x) for x in files]
        app.state.worker.kick()
        return {"found": len(res), "added": sum(r.get("added", 0) for r in res)}

    @app.post("/api/worker")
    def api_worker(body: dict = Body(...)):
        db.set_settings(db.get(), {"worker_paused": bool(body.get("paused"))})
        app.state.worker.kick()
        return {"paused": bool(body.get("paused"))}

    # ------------------------------------------------------------ videos
    @app.get("/api/videos/{vid}")
    def api_video(vid: str):
        con = db.get()
        r = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        if not r:
            raise HTTPException(404)
        d = db.row_to_dict(r)
        d["chunks"] = [dict(c) for c in con.execute("SELECT kind, t_start, t_end, text FROM chunks WHERE video_id=? ORDER BY t_start", (vid,))]
        return d

    @app.post("/api/videos/{vid}/note")
    def api_note(vid: str, body: dict = Body(...)):
        from . import pipeline
        con = db.get()
        if not con.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone():
            raise HTTPException(404)
        db.update(con, vid, note=(body.get("note") or "").strip() or None)
        pipeline.write_meta_chunks(con, vid, db.get_settings(con))
        return {"ok": True}

    @app.delete("/api/videos/{vid}")
    def api_delete(vid: str):
        con = db.get()
        db.delete_video(con, vid)
        shutil.rmtree(config.media_dir() / vid, ignore_errors=True)
        return {"ok": True}

    @app.post("/api/videos/{vid}/retry")
    def api_retry(vid: str):
        con = db.get()
        r = con.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
        if not r:
            raise HTTPException(404)
        db.set_status(con, vid, "new" if r["status"] in ("dead", "failed") else r["status"], attempts=0, error=None)
        app.state.worker.kick()
        return {"ok": True}

    # ------------------------------------------------------------ clusters
    @app.get("/api/clusters")
    def api_clusters():
        return {"clusters": clusters.list_clusters()}

    @app.post("/api/clusters/{cid}")
    def api_cluster_update(cid: int, body: dict = Body(...)):
        con = db.get()
        if "name" in body:
            con.execute("UPDATE clusters SET custom_name=? WHERE id=?", ((body["name"] or "").strip() or None, cid))
        if "pinned" in body:
            con.execute("UPDATE clusters SET pinned=? WHERE id=?", (1 if body["pinned"] else 0, cid))
        con.commit()
        return {"ok": True}

    @app.post("/api/recluster")
    def api_recluster():
        return clusters.recluster(db.get())

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    def api_settings():
        return db.get_settings()

    @app.post("/api/settings")
    def api_settings_set(body: dict = Body(...)):
        s = db.set_settings(db.get(), body)
        app.state.worker.kick()
        return s

    @app.post("/api/purge")
    def api_purge(body: dict = Body(...)):
        if body.get("confirm") != "PURGE":
            raise HTTPException(400, "type PURGE to confirm")
        w = app.state.worker
        w.stop()
        con = db.get()
        for t in ("vectors", "chunks", "videos", "clusters", "proc_stats", "search_log", "imports", "deleted"):
            con.execute(f"DELETE FROM {t}")
        con.commit()
        con.execute("INSERT INTO fts(fts) VALUES('rebuild')")
        con.commit()
        con.execute("VACUUM")
        shutil.rmtree(config.media_dir(), ignore_errors=True)
        shutil.rmtree(config.tmp_dir(), ignore_errors=True)
        shutil.rmtree(config.home() / "uploads", ignore_errors=True)
        db.bump_version()
        w.start()
        return {"ok": True}

    # ------------------------------------------------------------ export
    @app.get("/api/export/estimate")
    def api_export_estimate(cluster: str = "all", date_range: str = "all", formats: str = "json", ids: str = ""):
        con = db.get()
        sel = export.select_ids(con, cluster, date_range, [i for i in ids.split(",") if i] or None)
        return {"count": len(sel), "bytes": export.estimate_bytes(con, sel, formats.split(","))}

    @app.post("/api/export")
    def api_export(body: dict = Body(...)):
        con = db.get()
        sel = export.select_ids(con, body.get("cluster"), body.get("date_range"), body.get("ids") or None)
        fmt = body.get("format", "json")
        if fmt not in ("json", "csv"):
            raise HTTPException(400, "format must be json or csv")
        data = export.to_json(con, sel) if fmt == "json" else export.to_csv(con, sel)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        saved = export.write_export(con, sel, fmt) if body.get("save_copy") else None
        return Response(data, media_type="application/json" if fmt == "json" else "text/csv",
                        headers={"Content-Disposition": f'attachment; filename="recall-export-{stamp}.{fmt}"',
                                 "X-Recall-Count": str(len(sel)), **({"X-Recall-Saved": saved} if saved else {})})

    return app
