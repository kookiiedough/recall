"""Export collections as JSON (full) or CSV (flat). Optional MP4 clip export is personal-use only."""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime

from . import config, db


def select_ids(con, cluster: str | None = None, date_range: str | None = None, ids: list[str] | None = None) -> list[str]:
    where, args = ["status IN ('meta','deep')"], []
    if ids:
        where.append(f"id IN ({','.join('?'*len(ids))})"); args += ids
    if cluster and cluster != "all":
        where.append("cluster_id = ?"); args.append(int(cluster))
    days = {"24h": 1, "7d": 7, "30d": 30}.get(date_range or "")
    if days:
        where.append("saved_at >= ?"); args.append(time.time() - days * 86400)
    return [r[0] for r in con.execute(f"SELECT id FROM videos WHERE {' AND '.join(where)} ORDER BY saved_at DESC", args)]


def full_records(con, ids: list[str]) -> list[dict]:
    out = []
    for vid in ids:
        v = db.row_to_dict(con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())
        ch = con.execute("SELECT kind, t_start, t_end, text FROM chunks WHERE video_id=? ORDER BY kind, t_start", (vid,)).fetchall()
        cl = con.execute("SELECT coalesce(custom_name, auto_name) FROM clusters WHERE id=?", (v["cluster_id"],)).fetchone() if v["cluster_id"] else None
        out.append({
            "id": vid, "url": v["url"], "source": v["source"], "status": v["status"],
            "saved_at": _iso(v["saved_at"]), "creator": v["creator"], "creator_name": v["creator_name"],
            "caption": v["caption"], "sound": v["sound"], "duration": v["duration"], "hashtags": v["hashtags"],
            "cluster": cl[0] if cl else None, "note": v["note"],
            "attributes": {k: a for k, a in (v["attrs"] or {}).items()},
            "transcript": [{"t": c["t_start"], "text": c["text"]} for c in ch if c["kind"] == "transcript"],
            "on_screen_text": [{"t": c["t_start"], "text": c["text"]} for c in ch if c["kind"] == "ocr"],
        })
    return out


def _iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def to_json(con, ids: list[str]) -> bytes:
    return json.dumps({"exported_at": _iso(time.time()), "count": len(ids), "items": full_records(con, ids)},
                      ensure_ascii=False, indent=2).encode()


def to_csv(con, ids: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "url", "saved_at", "creator", "caption", "sound", "duration_s", "hashtags", "cluster", "note",
                "transcript_first_line", "status"])
    for r in full_records(con, ids):
        w.writerow([r["id"], r["url"], r["saved_at"], r["creator"], r["caption"], r["sound"], r["duration"],
                    " ".join("#" + h for h in r["hashtags"]), r["cluster"], r["note"],
                    r["transcript"][0]["text"] if r["transcript"] else "", r["status"]])
    return buf.getvalue().encode("utf-8-sig")


def estimate_bytes(con, ids: list[str], fmts: list[str]) -> int:
    if not ids:
        return 0
    sample = ids[:50]
    total = 0
    if "json" in fmts:
        total += int(len(to_json(con, sample)) * len(ids) / len(sample))
    if "csv" in fmts:
        total += int(len(to_csv(con, sample)) * len(ids) / len(sample))
    return total


def write_export(con, ids: list[str], fmt: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    data = to_json(con, ids) if fmt == "json" else to_csv(con, ids)
    p = config.exports_dir() / f"recall-export-{stamp}.{fmt}"
    p.write_bytes(data)
    return str(p)
