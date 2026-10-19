"""Hybrid retrieval: FTS5 BM25 + MiniLM chunk vectors + CLIP keyframe vectors, fused with RRF.

Every result carries `why`: the concrete signals (with raw scores, ranks and RRF contributions)
that put it there. Nothing in `why` is decorative.
"""
from __future__ import annotations

import difflib
import html
import json
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

import numpy as np

from . import db, fusion, models, queryparse, textutil
from .textutil import fmt_time

SEM_MIN = 0.36        # MiniLM cosine needed for a semantic hit (queries with >= 3 content words)
SEM_MIN_SHORT = 0.50  # 1-2 word queries: MiniLM similarities are inflated, so demand more
VIS_MIN = 0.235       # CLIP cosine needed for a visual-only hit ...
VIS_Z = 1.5           # ... and it must stand out from the corpus (z-score over videos)
WEIGHTS = {"keyword": 1.0, "semantic": 1.0, "visual": 0.8, "phrase": 1.0}
KIND_LABEL = {"caption": "Caption", "transcript": "Transcript", "ocr": "On-screen text", "creator": "Creator",
              "sound": "Sound", "hashtag": "Hashtag", "note": "Note"}
FILLER_RE = re.compile(r"^(?:(?:find|show|show me|search|search for|that|the|a|an)\s+)*(?:(?:video|tiktok|clip|one|post)s?\s+)?"
                       r"(?:(?:where|with|of|about|that|in which)\s+)?(?:(?:a|the|this|some)\s+)?"
                       r"(?:(?:guy|girl|man|woman|person|someone|lady|dude|creator)\s+)?(?:(?:says|said|saying|mentions|talks about)\s+)?", re.I)


# ------------------------------------------------------------------ vector cache
class VectorCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.key: tuple | None = None
        self.text: dict[str, Any] = {}
        self.image: dict[str, Any] = {}

    def ensure(self, con) -> None:
        key = (str(db.config.db_path()), db.data_version(),
               con.execute("SELECT count(*), coalesce(max(id),0) FROM vectors").fetchone()[:])
        with self.lock:
            if key == self.key:
                return
            for space, dim in (("text", 384), ("image", 512)):
                rows = con.execute("SELECT video_id, chunk_id, t, vec FROM vectors WHERE space=?", (space,)).fetchall()
                if rows:
                    M = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float16).reshape(len(rows), dim)
                else:
                    M = np.zeros((0, dim), dtype=np.float16)
                d = {"M": M, "vid": np.array([r["video_id"] for r in rows], dtype=object),
                     "chunk": np.array([r["chunk_id"] if r["chunk_id"] is not None else -1 for r in rows]),
                     "t": np.array([r["t"] if r["t"] is not None else np.nan for r in rows], dtype=np.float64)}
                setattr(self, space, d)
            self.key = key


CACHE = VectorCache()


def _best_per_video(sims: np.ndarray, vids: np.ndarray) -> dict[str, tuple[float, int]]:
    best: dict[str, tuple[float, int]] = {}
    order = np.argsort(-sims)
    for i in order:
        v = vids[i]
        if v not in best:
            best[v] = (float(sims[i]), int(i))
    return best


# ------------------------------------------------------------------ helpers
def _fts_query(terms: list[str], phrases: list[str], prefix_last: bool) -> str | None:
    parts = []
    for i, t in enumerate(terms):
        t = t.replace('"', "")
        if not t:
            continue
        if prefix_last and i == len(terms) - 1 and len(t) >= 3:
            parts.append(f'"{t}"*')
        else:
            parts.append(f'"{t}"')
    for p in phrases:
        toks = textutil.tokens(p)
        if toks:
            parts.append('"' + " ".join(toks) + '"')
    return " OR ".join(parts) if parts else None


def _snippet(hl: str, width: int = 140) -> str:
    """hl contains \x01..\x02 markers; return html with <mark>, trimmed around the first hit."""
    i = hl.find("\x01")
    if len(hl) > width and i > width // 3:
        hl = "…" + hl[i - width // 3:]
    if len(hl) > width:
        cut = hl[:width]
        if cut.count("\x01") > cut.count("\x02"):
            cut += "\x02"
        hl = cut + "…"
    return html.escape(hl).replace("\x01", "<mark>").replace("\x02", "</mark>")


def _plain_snippet(text: str, width: int = 140) -> str:
    return html.escape(text[:width] + ("…" if len(text) > width else ""))


def _word_time(meta: str | None, phrase_tokens: list[str], fallback: float | None) -> float | None:
    """Locate a phrase / term inside a transcript segment using word timestamps."""
    if not meta or not phrase_tokens:
        return fallback
    try:
        words = json.loads(meta).get("words") or []
    except Exception:
        return fallback
    toks = [re.sub(r"[^\w']", "", w["w"].lower()) for w in words]
    first = phrase_tokens[0]
    for i, w in enumerate(toks):
        if w == first or (len(first) > 3 and w.startswith(first[:4])):
            return words[i]["t"]
    return fallback


def _live_filter_sql(pq: queryparse.ParsedQuery, facets: dict) -> tuple[str, list]:
    where, args = ["v.status IN ('meta','partial','deep')"], []
    for c in pq.creators + facets.get("creator", []):
        where.append("(v.creator = ? OR v.creator LIKE ?)")
        args += [c.lower().lstrip("@"), c.lower().lstrip("@") + "%"]
    for h in pq.hashtags + facets.get("hashtag", []):
        where.append("EXISTS (SELECT 1 FROM json_each(v.hashtags) j WHERE j.value = ?)")
        args.append(h.lower().lstrip("#"))
    for s in facets.get("sound", []):
        where.append("v.sound = ?")
        args.append(s)
    for cl in facets.get("cluster", []):
        where.append("v.cluster_id = ?")
        args.append(int(cl))
    for a in facets.get("attr", []):
        where.append("json_extract(v.attrs, ?) = 1")
        args.append(f"$.{re.sub(r'[^a-z_]', '', a)}.value")
    if pq.date_from is not None:
        where.append("v.saved_at >= ?"); args.append(pq.date_from)
    if pq.date_to is not None:
        where.append("v.saved_at < ?"); args.append(pq.date_to)
    for d in facets.get("saved", []):
        a, b = SAVED_BUCKETS_FN(d)
        where.append("v.saved_at >= ? AND v.saved_at < ?"); args += [a, b]
    if pq.dur_min is not None:
        where.append("v.duration >= ?"); args.append(pq.dur_min)
    if pq.dur_max is not None:
        where.append("v.duration <= ?"); args.append(pq.dur_max)
    for d in facets.get("duration", []):
        lo, hi = DURATION_BUCKETS[d]
        where.append("v.duration >= ? AND v.duration < ?"); args += [lo, hi]
    return " AND ".join(where), args


DURATION_BUCKETS = {"< 15s": (0, 15), "15–60s": (15, 60), "1–3 min": (60, 180), "> 3 min": (180, 1e9)}


def SAVED_BUCKETS_FN(label: str) -> tuple[float, float]:
    now = time.time()
    if label == "Last 24 hours":
        return now - 86400, now + 1
    if label == "This week":
        return now - 7 * 86400, now + 1
    if label == "This month":
        return now - 30 * 86400, now + 1
    m = re.match(r"(\w+) (\d{4})$", label)
    if m:
        mon = list(queryparse.calendar.month_name).index(m.group(1))
        return queryparse._month_range(int(m.group(2)), mon)
    return 0, now + 1


# ------------------------------------------------------------------ main entry
def search(q: str, facets: dict | None = None, limit: int = 60, offset: int = 0, settings: dict | None = None,
           con=None, now: datetime | None = None, log: bool = True) -> dict[str, Any]:
    t_start = time.perf_counter()
    con = con or db.get()
    facets = facets or {}
    settings = settings or db.get_settings(con)
    pq = queryparse.parse(q or "", now=now)
    timing: dict[str, float] = {}

    where, args = _live_filter_sql(pq, facets)
    vids_rows = con.execute(f"SELECT v.id, v.saved_at, v.creator FROM videos v WHERE {where}", args).fetchall()
    allowed = {r["id"]: r for r in vids_rows}
    total_live = con.execute("SELECT count(*) FROM videos WHERE status IN ('meta','partial','deep')").fetchone()[0]

    sem_text = FILLER_RE.sub("", pq.text).strip() or pq.text
    if pq.speech:
        sem_text = pq.speech[0]
    terms = list(dict.fromkeys(textutil.content_terms(pq.text)))
    phrases = pq.phrases + pq.speech
    has_text = bool(terms or phrases or sem_text)

    why: dict[str, list[dict]] = defaultdict(list)
    lists: dict[str, list[str]] = {}
    best_t: dict[str, tuple[int, float, float]] = {}   # vid -> (priority, strength, t)

    def offer_t(v: str, prio: int, strength: float, t: float | None) -> None:
        if t is None or (isinstance(t, float) and np.isnan(t)):
            return
        cur = best_t.get(v)
        if cur is None or (prio, strength) > (cur[0], cur[1]):
            best_t[v] = (prio, strength, float(t))

    # ---------------- keyword (FTS5 BM25)
    t0 = time.perf_counter()
    kw_score: dict[str, float] = defaultdict(float)
    phrase_score: dict[str, float] = {}
    if allowed and has_text:
        fq = _fts_query(terms, [], prefix_last=not (q or "").endswith(" "))
        if fq:
            rows = con.execute(
                "SELECT c.id, c.video_id, c.kind, c.t_start, c.text, c.meta, bm25(fts) AS s, "
                "highlight(fts, 0, char(1), char(2)) AS hl FROM fts JOIN chunks c ON c.id = fts.rowid "
                "WHERE fts MATCH ? ORDER BY s LIMIT 5000", (fq,)).fetchall()
            per_kind: dict[tuple[str, str], Any] = {}
            for r in rows:
                if r["video_id"] not in allowed:
                    continue
                k = (r["video_id"], r["kind"])
                if k not in per_kind:
                    per_kind[k] = r          # rows are bm25-ordered, first = best per (video, kind)
            for (v, kind), r in per_kind.items():
                s = -float(r["s"])
                kw_score[v] += s
                matched = re.findall(r"\x01(.*?)\x02", r["hl"])
                t = r["t_start"]
                if kind == "transcript":
                    t = _word_time(r["meta"], [textutil.tokens(m)[0] for m in matched if textutil.tokens(m)][:1], t)
                why[v].append({"signal": kind, "label": KIND_LABEL[kind], "method": "keyword", "score": round(s, 3),
                               "metric": "bm25", "t": t, "snippet": _snippet(r["hl"]), "terms": sorted(set(m.lower() for m in matched))})
                offer_t(v, 3 if kind in ("transcript", "ocr") else 0, s, t)
        # exact phrases (quoted, or "says X" -> transcript only)
        for ph, speech_only in [(p, False) for p in pq.phrases] + [(p, True) for p in pq.speech]:
            toks = textutil.tokens(ph)
            if not toks:
                continue
            rows = con.execute(
                "SELECT c.id, c.video_id, c.kind, c.t_start, c.meta, bm25(fts) AS s, highlight(fts, 0, char(1), char(2)) AS hl "
                "FROM fts JOIN chunks c ON c.id = fts.rowid WHERE fts MATCH ? ORDER BY s LIMIT 1000",
                ('"' + " ".join(toks) + '"',)).fetchall()
            for r in rows:
                if r["video_id"] not in allowed or (speech_only and r["kind"] != "transcript"):
                    continue
                v = r["video_id"]
                if v in phrase_score:
                    continue
                s = -float(r["s"])
                phrase_score[v] = s
                t = _word_time(r["meta"], toks, r["t_start"]) if r["kind"] == "transcript" else r["t_start"]
                why[v].append({"signal": r["kind"], "label": KIND_LABEL[r["kind"]] + " phrase", "method": "phrase",
                               "score": round(s, 3), "metric": "bm25", "t": t, "snippet": _snippet(r["hl"]),
                               "terms": [ph]})
                offer_t(v, 5, s, t)
    lists["keyword"] = sorted(kw_score, key=lambda v: -kw_score[v])
    if phrase_score:
        lists["phrase"] = sorted(phrase_score, key=lambda v: -phrase_score[v])
    timing["keyword_ms"] = (time.perf_counter() - t0) * 1000

    # ---------------- semantic (MiniLM over chunks) and visual (CLIP over keyframes)
    sem_best: dict[str, tuple[float, int]] = {}
    vis_best: dict[str, tuple[float, int]] = {}
    vis_stats = (0.0, 1.0)
    if allowed and sem_text:
        CACHE.ensure(con)
        T = CACHE.text
        if settings.get("text_embeddings", True) and len(T["M"]):
            t0 = time.perf_counter()
            qv = models.embed_text([sem_text])[0]
            sims = T["M"].astype(np.float32) @ qv
            mask = np.array([v in allowed for v in T["vid"]])
            sims = np.where(mask, sims, -1.0)
            sem_best = _best_per_video(sims, T["vid"])
            sem_best = {v: x for v, x in sem_best.items() if v in allowed}
            timing["semantic_ms"] = (time.perf_counter() - t0) * 1000
        I = CACHE.image
        if settings.get("visual", True) and len(I["M"]):
            t0 = time.perf_counter()
            qv = models.clip_text([f"a photo of {sem_text}"])[0]
            sims = I["M"].astype(np.float32) @ qv
            vis_all = _best_per_video(sims, I["vid"])
            allv = np.array([x[0] for x in vis_all.values()])
            vis_stats = (float(allv.mean()), float(allv.std() + 1e-6)) if len(allv) else (0.0, 1.0)
            vis_best = {v: x for v, x in vis_all.items() if v in allowed}
            timing["visual_ms"] = (time.perf_counter() - t0) * 1000

    chunk_cache: dict[int, Any] = {}

    def chunk(cid: int):
        if cid not in chunk_cache:
            chunk_cache[cid] = con.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone()
        return chunk_cache[cid]

    sem_min = SEM_MIN if len(terms) >= 3 else SEM_MIN_SHORT
    sem_hits = [v for v, (s, _) in sem_best.items() if s >= sem_min]
    sem_hits.sort(key=lambda v: -sem_best[v][0])
    lists["semantic"] = sem_hits
    for v in sem_hits:
        s, i = sem_best[v]
        c = chunk(int(CACHE.text["chunk"][i]))
        if c is None:
            continue
        why[v].append({"signal": c["kind"], "label": KIND_LABEL.get(c["kind"], c["kind"]) + " (meaning)", "method": "semantic",
                       "score": round(s, 3), "metric": "cosine", "t": c["t_start"], "snippet": _plain_snippet(c["text"])})
        offer_t(v, 2, s, c["t_start"])
    mu, sd = vis_stats
    vis_hits = [v for v, (s, _) in vis_best.items() if s >= VIS_MIN and (len(vis_best) < 5 or (s - mu) / sd >= VIS_Z)]
    vis_hits.sort(key=lambda v: -vis_best[v][0])
    lists["visual"] = vis_hits
    for v in vis_hits:
        s, i = vis_best[v]
        t = CACHE.image["t"][i]
        t = None if np.isnan(t) else float(t)
        why[v].append({"signal": "visual", "label": "Visual similarity", "method": "visual", "score": round(s, 3),
                       "metric": "clip cosine", "z": round((s - mu) / sd, 2), "t": t,
                       "snippet": html.escape(f'frame {"@ " + fmt_time(t) if t is not None else "(thumbnail)"} ~ "{sem_text}"')})
        offer_t(v, 1, s, t)

    # ---------------- fusion
    if has_text:
        scores, contrib = fusion.rrf({k: v for k, v in lists.items() if v}, WEIGHTS)
    else:  # filter-only query: everything allowed, newest save first
        order = sorted(allowed, key=lambda v: -(allowed[v]["saved_at"] or 0))
        scores, contrib = fusion.rrf({"filter": order}, {"filter": 1.0})
    # boosts: recency of save and creator affinity (how many of your saves are from this creator)
    creator_counts = Counter(r[0] for r in con.execute(
        "SELECT creator FROM videos WHERE status IN ('meta','partial','deep') AND creator IS NOT NULL"))
    max_cc = max(creator_counts.values()) if creator_counts else 1
    nowts = (now or datetime.now()).timestamp()
    boosts: dict[str, dict[str, float]] = {}
    for v in scores:
        r = allowed[v]
        age = (nowts - r["saved_at"]) / 86400 if r["saved_at"] else None
        boosts[v] = {"recency": fusion.recency_boost(age),
                     "creator_affinity": fusion.affinity_boost(creator_counts.get(r["creator"], 0), max_cc)}
    final = fusion.apply_boosts(scores, boosts)
    ranked = sorted(final, key=lambda v: -final[v])
    timing["total_ms"] = (time.perf_counter() - t_start) * 1000

    filt_chips = pq.filters()
    page = ranked[offset:offset + limit]
    results = [_result(con, v, final[v], contrib[v], boosts[v], why.get(v, []), best_t.get(v), filt_chips, facets)
               for v in page]
    ms = (time.perf_counter() - t_start) * 1000
    out: dict[str, Any] = {
        "query": q, "parsed": pq.to_dict(), "semantic_text": sem_text, "total": len(ranked), "offset": offset,
        "results": results, "timing": {**{k: round(x, 1) for k, x in timing.items()}, "total_ms": round(ms, 1)},
        "candidates": len(allowed), "index_size": total_live,
        # zero hits -> show whole-index facets so the sidebar still offers ways out
        "facets": facet_counts(con, ranked if (ranked and (has_text or facets or filt_chips)) else None),
    }
    if not ranked:
        out["fuzzy"] = _fuzzy(con, sem_best, vis_best, mu, sd)
        out["suggestions"] = _suggestions(con, pq, terms, facets, settings)
    if log:
        con.execute("INSERT INTO search_log(query, ms, n, at) VALUES (?,?,?,?)", (q, ms, len(ranked), time.time()))
        con.commit()
    return out


def _result(con, vid: str, score: float, contrib: dict, boosts: dict, why: list, bt, filt_chips, facets) -> dict:
    v = db.row_to_dict(con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())
    for w in why:
        lst = {"keyword": "keyword", "phrase": "phrase", "semantic": "semantic", "visual": "visual"}[w["method"]]
        if lst in contrib:
            w["rank"] = contrib[lst]["rank"]
            # a video's keyword-list contribution is shared by all its keyword chips
            w["rrf"] = round(contrib[lst]["contrib"], 5)
    why.sort(key=lambda w: (-(w.get("rrf") or 0), -w["score"]))
    for f in filt_chips:
        why.append({"signal": "filter", "label": f["label"], "method": "filter", "score": None,
                    "snippet": _filter_evidence(v, f)})
    for k, vals in facets.items():
        for val in vals:
            why.append({"signal": "filter", "label": f"{k}: {_facet_label(con, k, val)}", "method": "facet", "score": None, "snippet": ""})
    boost_list = [{"name": k, "value": round(x, 4)} for k, x in boosts.items() if x > 0]
    return {
        "id": vid, "url": v["url"], "source": v["source"], "status": v["status"], "caption": v["caption"],
        "error": v["error"] if v["status"] == "partial" else None,
        "creator": v["creator"], "creator_name": v["creator_name"], "sound": v["sound"], "duration": v["duration"],
        "saved_at": v["saved_at"], "hashtags": v["hashtags"], "thumb": v["thumb"], "frames": v["frames"],
        "note": v["note"], "attrs": {k: a for k, a in (v["attrs"] or {}).items() if a.get("value")},
        "cluster_id": v["cluster_id"], "score": round(score, 5), "rrf": {k: {"rank": c["rank"], "contrib": round(c["contrib"], 5)} for k, c in contrib.items()},
        "boosts": boost_list, "best_t": bt[2] if bt else None, "why": why,
    }


def _facet_label(con, k: str, val: str) -> str:
    if k == "cluster":
        r = con.execute("SELECT coalesce(custom_name, auto_name) FROM clusters WHERE id=?", (int(val),)).fetchone()
        return r[0] if r else str(val)
    return str(val)


def _filter_evidence(v: dict, f: dict) -> str:
    if f["type"] == "creator":
        return html.escape(f"@{v['creator']}")
    if f["type"] == "hashtag":
        return html.escape("#" + f["value"])
    if f["type"] == "date":
        return html.escape("saved " + (datetime.fromtimestamp(v["saved_at"]).strftime("%b %d, %Y") if v["saved_at"] else "?"))
    if f["type"] == "duration":
        return html.escape(f"{v['duration']:.0f}s long" if v["duration"] else "")
    return ""


def _fuzzy(con, sem_best, vis_best, mu, sd, n: int = 6) -> list[dict]:
    cands: dict[str, dict] = {}
    for v, (s, i) in sem_best.items():
        c = con.execute("SELECT kind, text FROM chunks WHERE id=?", (int(CACHE.text["chunk"][i]),)).fetchone()
        cands[v] = {"id": v, "score": round(s, 3), "signal": f"meaning · {KIND_LABEL.get(c['kind'], '') if c else ''}",
                    "detail": (c["text"][:80] if c else ""), "metric": "MiniLM cosine"}
    for v, (s, i) in vis_best.items():
        if v not in cands or s > cands[v]["score"]:
            cands[v] = {"id": v, "score": round(s, 3), "signal": "visual", "detail": f"z={(s-mu)/sd:.2f}", "metric": "CLIP cosine"}
    top = sorted(cands.values(), key=lambda x: -x["score"])[:n]
    for x in top:
        r = con.execute("SELECT creator, thumb, caption, url FROM videos WHERE id=?", (x["id"],)).fetchone()
        x.update(dict(r))
    return top


def _suggestions(con, pq, terms, facets, settings) -> dict:
    broaden, help_ = [], []
    vocab_rows = None
    for t in terms:
        stem = t
        n = con.execute("SELECT count(DISTINCT c.video_id) FROM fts JOIN chunks c ON c.id=fts.rowid WHERE fts MATCH ?",
                        (f'"{stem}"',)).fetchone()[0]
        if n == 0:
            if vocab_rows is None:
                vocab_rows = [r[0] for r in con.execute("SELECT term FROM fts_vocab WHERE doc >= 1 LIMIT 50000")]
            close = [c for c in difflib.get_close_matches(t, vocab_rows, n=3, cutoff=0.8) if c[:1] == t[:1]]
            if close:
                broaden.append({"text": f'Did you mean "{close[0]}"? ("{t}" appears in 0 saves)', "query": " ".join(close[0] if x == t else x for x in pq.text.split())})
            else:
                broaden.append({"text": f'Try removing "{t}" (appears in 0 saves)', "query": " ".join(x for x in pq.text.split() if x.lower() != t)})
    for f in pq.filters():
        if f["type"] != "speech":
            broaden.append({"text": f"Remove the filter {f['label']}", "query": pq.text})
    if facets:
        broaden.append({"text": "Clear sidebar facets", "query": pq.raw, "clear_facets": True})
    if len(terms) > 2:
        broaden.append({"text": f'Use fewer words, e.g. "{" ".join(terms[:2])}"', "query": " ".join(terms[:2])})
    counts = dict(con.execute("SELECT status, count(*) FROM videos GROUP BY status").fetchall())
    if counts.get("meta"):
        help_.append(f"{counts['meta']} saves are not deep-indexed yet (no transcript/OCR/visual) — see Import queue")
    if counts.get("partial"):
        help_.append(f"{counts['partial']} saves couldn't be downloaded, so only their caption + thumbnail are indexed (no transcript) — see Import queue")
    if counts.get("new"):
        help_.append(f"{counts['new']} saves are still waiting for metadata")
    for k, name in (("ocr", "On-screen text (OCR)"), ("transcripts", "Transcripts"), ("visual", "Visual similarity")):
        if not settings.get(k, True):
            help_.append(f"{name} indexing is off — enable it in Settings")
    if counts.get("dead"):
        help_.append(f"{counts['dead']} saves are dead links (deleted or private) and can't be searched")
    if not help_:
        help_.append("Every save is fully indexed; this content may simply not be in your saves")
    return {"broaden": broaden[:4], "help": help_[:4]}


# ------------------------------------------------------------------ facets
def facet_counts(con, ids: list[str] | None, top: int = 8) -> dict[str, list]:
    if ids is None:
        rows = con.execute("SELECT * FROM videos WHERE status IN ('meta','partial','deep')").fetchall()
    else:
        rows = []
        for i in range(0, len(ids), 900):
            part = ids[i:i + 900]
            rows += con.execute(f"SELECT * FROM videos WHERE id IN ({','.join('?'*len(part))})", part).fetchall()
    creators, sounds, tags, clusters, attrs_c, saved, durs = Counter(), Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    now = time.time()
    for r in rows:
        if r["creator"]:
            creators[r["creator"]] += 1
        if r["sound"]:
            sounds[r["sound"]] += 1
        for h in json.loads(r["hashtags"] or "[]"):
            tags[h] += 1
        if r["cluster_id"] is not None:
            clusters[r["cluster_id"]] += 1
        for k, a in json.loads(r["attrs"] or "{}").items():
            if a.get("value"):
                attrs_c[k] += 1
        if r["saved_at"]:
            age = now - r["saved_at"]
            if age < 86400:
                saved["Last 24 hours"] += 1
            if age < 7 * 86400:
                saved["This week"] += 1
            if age < 30 * 86400:
                saved["This month"] += 1
            saved[datetime.fromtimestamp(r["saved_at"]).strftime("%B %Y")] += 1
        if r["duration"]:
            for lab, (lo, hi) in DURATION_BUCKETS.items():
                if lo <= r["duration"] < hi:
                    durs[lab] += 1
    cl_names = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, coalesce(custom_name, auto_name), pinned FROM clusters")}
    cl = [{"value": str(k), "label": cl_names.get(k, (f"Cluster {k}", 0))[0], "count": n, "pinned": bool(cl_names.get(k, ("", 0))[1])}
          for k, n in clusters.items()]
    cl.sort(key=lambda x: (-x["pinned"], -x["count"]))
    months = sorted([k for k in saved if re.match(r"\w+ \d{4}$", k)], key=lambda k: -datetime.strptime(k, "%B %Y").timestamp())
    saved_list = [{"value": k, "label": k, "count": saved[k]} for k in ["Last 24 hours", "This week", "This month"] if saved.get(k)]
    saved_list += [{"value": k, "label": k, "count": saved[k]} for k in months[:4]]
    attr_names = {"has_recipe": "Has recipe", "has_product_link": "Has product link", "talking_head": "Talking head",
                  "screen_recording": "Screen recording"}
    return {
        "cluster": cl[:top],
        "attr": [{"value": k, "label": attr_names.get(k, k), "count": attrs_c.get(k, 0)} for k in attr_names],
        "creator": [{"value": k, "label": "@" + k, "count": n} for k, n in creators.most_common(top)],
        "sound": [{"value": k, "label": k, "count": n} for k, n in sounds.most_common(top)],
        "hashtag": [{"value": k, "label": "#" + k, "count": n} for k, n in tags.most_common(top)],
        "saved": saved_list,
        "duration": [{"value": k, "label": k, "count": durs[k]} for k in DURATION_BUCKETS if durs.get(k)],
    }
