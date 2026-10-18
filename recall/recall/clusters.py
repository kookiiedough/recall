"""Automatic topic clusters (KMeans over per-video embeddings), named from c-TF-IDF keywords/hashtags.
Renames and pins survive re-clustering by matching new clusters to old ones by member overlap."""
from __future__ import annotations

import json
import math
import time
from collections import Counter

import numpy as np

from . import db, textutil

CATEGORIES = {
    "Recipes": {"recipe", "pasta", "cook", "cooking", "food", "dinner", "bake", "baking", "feta", "foodtok", "easyrecipe",
                "sauce", "oven", "chicken", "dessert", "cake", "ingredients", "lunch", "breakfast", "meal", "tacos", "kitchen"},
    "Fitness": {"workout", "gym", "fitness", "exercise", "squat", "squats", "cardio", "abs", "fitnesstok", "training", "reps",
                "muscle", "yoga", "run", "running", "stretch", "pilates"},
    "Travel": {"travel", "trip", "vacation", "beach", "city", "hotel", "flight", "traveltok", "explore", "tour", "island",
               "vanlife", "roadtrip", "hike", "hiking", "mountains", "sunset"},
    "Beauty & Fashion": {"grwm", "makeup", "outfit", "dress", "fashion", "skincare", "ootd", "style", "hair", "beauty", "lipstick"},
    "Tech & Tips": {"phone", "iphone", "app", "tech", "hack", "tips", "laptop", "settings", "lifehack", "productivity", "code", "charger"},
    "Home": {"home", "decor", "interior", "cleaning", "room", "diy", "organize", "plants", "apartment", "furniture"},
    "Pets": {"dog", "cat", "puppy", "pets", "petsoftiktok", "kitten"},
    "Money": {"money", "finance", "budget", "invest", "investing", "savings", "debt"},
    "Comedy": {"funny", "comedy", "joke", "prank", "meme", "lol"},
}


def _features(con) -> tuple[list[str], np.ndarray, dict[str, str]]:
    vids = [r[0] for r in con.execute("SELECT id FROM videos WHERE status IN ('meta','deep')")]
    if not vids:
        return [], np.zeros((0, 896)), {}
    text_sum: dict[str, np.ndarray] = {}
    img_sum: dict[str, np.ndarray] = {}
    for r in con.execute("SELECT video_id, space, vec FROM vectors"):
        v = np.frombuffer(r["vec"], dtype=np.float16).astype(np.float32)
        d = text_sum if r["space"] == "text" else img_sum
        d[r["video_id"]] = d.get(r["video_id"], 0) + v
    docs = {}
    for r in con.execute("SELECT video_id, group_concat(text, ' ') FROM chunks WHERE kind IN "
                         "('caption','hashtag','transcript','ocr','note') GROUP BY video_id"):
        docs[r[0]] = r[1] or ""
    keep, feats = [], []
    for v in vids:
        if v not in text_sum and v not in img_sum:
            continue
        t = text_sum.get(v, np.zeros(384)); i = img_sum.get(v, np.zeros(512))
        t = t / (np.linalg.norm(t) + 1e-9); i = i / (np.linalg.norm(i) + 1e-9)
        feats.append(np.concatenate([t, 0.5 * i]))
        keep.append(v)
    return keep, np.array(feats, dtype=np.float32), docs


def name_cluster(keywords: list[str], hashtags: list[str], member_tokens: list[set] | None = None) -> str:
    """Use a broad category name only when >= 40% of the members actually contain one of its words;
    otherwise name the cluster after its two most distinctive keywords/hashtags."""
    if member_tokens:
        best, share = None, 0.0
        for cat, lex in CATEGORIES.items():
            frac = sum(1 for t in member_tokens if t & lex) / len(member_tokens)
            if frac > share:
                best, share = cat, frac
        if best and share >= 0.4:
            return best
    else:
        bag = Counter()
        for w in keywords[:10] + hashtags[:10]:
            for cat, lex in CATEGORIES.items():
                if w in lex:
                    bag[cat] += 1
        if bag and bag.most_common(1)[0][1] >= 2:
            return bag.most_common(1)[0][0]
    words = [w for w in (hashtags[:1] + keywords) if w][:2]
    return " · ".join(w.capitalize() for w in dict.fromkeys(words)) or "Misc"


def recluster(con=None, k: int | None = None, seed: int = 0) -> dict:
    from sklearn.cluster import KMeans
    con = con or db.get()
    vids, X, docs = _features(con)
    n = len(vids)
    if n < 4:
        return {"clusters": 0, "videos": n, "skipped": "need at least 4 indexed videos"}
    k = k or max(2, min(20, int(round(math.sqrt(n) if n < 200 else math.sqrt(n / 2)))))
    k = min(k, n)
    labels = KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(X)
    # c-TF-IDF keywords per cluster
    cl_tokens: dict[int, Counter] = {c: Counter() for c in range(k)}
    cl_tags: dict[int, Counter] = {c: Counter() for c in range(k)}
    cl_members_tok: dict[int, list[set]] = {c: [] for c in range(k)}
    tagmap = {r[0]: json.loads(r[1] or "[]") for r in con.execute("SELECT id, hashtags FROM videos")}
    for v, c in zip(vids, labels):
        toks = set(textutil.content_terms(docs.get(v, ""))) | set(tagmap.get(v, []))
        cl_tokens[c].update(toks)
        cl_tags[c].update(tagmap.get(v, []))
        cl_members_tok[c].append(toks)
    df = Counter()
    for c in cl_tokens:
        df.update(set(cl_tokens[c]))
    members = {c: {v for v, l in zip(vids, labels) if l == c} for c in range(k)}
    new = []
    for c in range(k):
        size = len(members[c])
        scored = {w: (cnt / size) * math.log(1 + k / df[w]) for w, cnt in cl_tokens[c].items() if cnt >= 1 and not w.isdigit() and len(w) >= 3}
        kws = [w for w, _ in sorted(scored.items(), key=lambda x: -x[1])[:8]]
        tags = [t for t, _ in cl_tags[c].most_common(5) if t not in ("fyp", "foryou", "foryoupage", "viral", "fy")]
        new.append({"members": members[c], "keywords": kws, "tags": tags, "name": name_cluster(kws, tags, cl_members_tok[c])})
    # make names unique: the larger cluster keeps a shared name, others get "Name · keyword"
    seen: dict[str, int] = {}
    for nc in sorted(new, key=lambda x: -len(x["members"])):
        if nc["name"] in seen:
            extra = next((w for w in nc["tags"] + nc["keywords"] if w.lower() not in nc["name"].lower()), None)
            nc["name"] = f"{nc['name']} · {extra.capitalize()}" if extra else f"{nc['name']} {seen[nc['name']] + 1}"
        seen[nc["name"]] = seen.get(nc["name"], 0) + 1
    # match to old clusters to preserve custom names / pins
    old = {r["id"]: dict(r) for r in con.execute("SELECT * FROM clusters")}
    old_members: dict[int, set] = {cid: set() for cid in old}
    for r in con.execute("SELECT id, cluster_id FROM videos WHERE cluster_id IS NOT NULL"):
        old_members.setdefault(r[1], set()).add(r[0])
    pairs = []
    for i, nc in enumerate(new):
        for cid, mem in old_members.items():
            if cid in old and mem:
                j = len(nc["members"] & mem) / len(nc["members"] | mem)
                if j >= 0.3:
                    pairs.append((j, i, cid))
    pairs.sort(reverse=True)
    used_new, used_old, assign = set(), set(), {}
    for j, i, cid in pairs:
        if i not in used_new and cid not in used_old:
            assign[i] = cid; used_new.add(i); used_old.add(cid)
    con.execute("UPDATE videos SET cluster_id=NULL")
    for cid in old:
        if cid not in used_old:
            if old[cid]["pinned"]:
                con.execute("UPDATE clusters SET size=0 WHERE id=?", (cid,))
            else:
                con.execute("DELETE FROM clusters WHERE id=?", (cid,))
    for i, nc in enumerate(new):
        kw = json.dumps(nc["tags"][:3] + [w for w in nc["keywords"] if w not in nc["tags"]][:6])
        if i in assign:
            cid = assign[i]
            con.execute("UPDATE clusters SET auto_name=?, keywords=?, size=? WHERE id=?", (nc["name"], kw, len(nc["members"]), cid))
        else:
            cid = con.execute("INSERT INTO clusters(auto_name, keywords, size, created_at) VALUES (?,?,?,?)",
                              (nc["name"], kw, len(nc["members"]), time.time())).lastrowid
        con.executemany("UPDATE videos SET cluster_id=? WHERE id=?", [(cid, v) for v in nc["members"]])
    con.commit()
    db.bump_version()
    return {"clusters": k, "videos": n}


def list_clusters(con=None) -> list[dict]:
    con = con or db.get()
    out = []
    for r in con.execute("SELECT * FROM clusters ORDER BY pinned DESC, size DESC"):
        d = dict(r)
        d["keywords"] = json.loads(d["keywords"] or "[]")
        d["name"] = d["custom_name"] or d["auto_name"]
        d["samples"] = [dict(x) for x in con.execute(
            "SELECT id, thumb, caption, creator FROM videos WHERE cluster_id=? ORDER BY saved_at DESC LIMIT 6", (r["id"],))]
        out.append(d)
    return out
