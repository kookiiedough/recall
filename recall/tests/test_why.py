"""why-matched output must reflect the real signals behind a rank (no models needed: keyword + filters)."""
import json
import time

import pytest

from recall import db, pipeline, search
from conftest import NO_MODELS

S = dict(NO_MODELS)


def mk(con, vid, caption, creator, transcript=(), ocr=(), sound=None, days_ago=10, duration=30.0, tags=None):
    db.upsert_new_video(con, vid, f"https://www.tiktok.com/@{creator}/video/{vid}", time.time() - days_ago * 86400)
    db.set_status(con, vid, "meta", caption=caption, creator=creator, sound=sound, duration=duration,
                  hashtags=tags if tags is not None else [t.strip("#").lower() for t in caption.split() if t.startswith("#")])
    pipeline.write_meta_chunks(con, vid, S)
    rows = [("transcript", t0, t0 + 3, text, {"words": [{"w": w, "t": round(t0 + i * 0.4, 2)} for i, w in enumerate(text.split())]})
            for t0, text in transcript]
    rows += [("ocr", t0, t0, text, None) for t0, text in ocr]
    db.replace_chunks(con, vid, ("transcript", "ocr"), rows)
    db.set_status(con, vid, "deep")


@pytest.fixture()
def corpus(con):
    mk(con, "1", "Baked feta pasta #pasta #recipe", "chef_marta", transcript=[(4.0, "add the tomatoes then the feta block")], days_ago=2)
    mk(con, "2", "you need to hear this #techtips", "techdad", transcript=[(0.5, "okay listen"), (6.0, "stop doing this with your charger")])
    mk(con, "3", "LA day two #travel", "nomadnina", ocr=[(5.0, "SUNSET BOULEVARD WALKING TOUR")], sound="Sunny Days — Lofi")
    mk(con, "4", "study with me", "lena", sound="Midnight Coffee — Lena Lofi", days_ago=200, duration=7200)
    return con


def q(con, text, **kw):
    return search.search(text, con=con, settings=S, log=False, **kw)


def test_caption_match_has_only_caption_chips(corpus):
    r = q(corpus, "baked pasta")
    top = r["results"][0]
    assert top["id"] == "1"
    kinds = {w["signal"] for w in top["why"]}
    assert kinds <= {"caption", "hashtag"} and "caption" in kinds
    cap = next(w for w in top["why"] if w["signal"] == "caption")
    assert cap["metric"] == "bm25" and cap["score"] > 0 and "<mark>" in cap["snippet"] and cap["rank"] == 1
    assert "transcript" not in kinds and "visual" not in kinds


def test_transcript_phrase_timestamp_from_words(corpus):
    r = q(corpus, "video where a guy says stop doing this")
    top = r["results"][0]
    assert top["id"] == "2"
    ph = next(w for w in top["why"] if w["method"] == "phrase")
    assert ph["signal"] == "transcript" and ph["t"] == 6.0
    assert top["best_t"] == 6.0
    assert any(w["method"] == "filter" and "stop doing this" in w["label"] for w in top["why"])


def test_word_level_timestamp_inside_segment(corpus):
    top = q(corpus, "feta block")["results"][0]
    tr = next(w for w in top["why"] if w["signal"] == "transcript")
    assert tr["t"] == pytest.approx(4.0 + 5 * 0.4)    # "feta" is word index 5 of the segment
    assert top["best_t"] == tr["t"]


def test_ocr_chip_and_timestamp(corpus):
    top = q(corpus, "sunset boulevard")["results"][0]
    assert top["id"] == "3"
    w = top["why"][0]
    assert w["signal"] == "ocr" and w["label"] == "On-screen text" and w["t"] == 5.0
    assert top["best_t"] == 5.0


def test_sound_and_creator_chips(corpus):
    assert q(corpus, "midnight coffee")["results"][0]["why"][0]["signal"] == "sound"
    top = q(corpus, "nomadnina")["results"][0]
    assert top["id"] == "3" and top["why"][0]["signal"] == "creator"


def test_filter_only_queries_explain_the_filter(corpus):
    r = q(corpus, "@techdad")
    assert [x["id"] for x in r["results"]] == ["2"]
    assert r["results"][0]["why"] == [{"signal": "filter", "label": "@techdad", "method": "filter", "score": None, "snippet": "@techdad"}]
    r = q(corpus, "#travel")
    assert [x["id"] for x in r["results"]] == ["3"]
    r = q(corpus, "saved past 5 days")
    assert [x["id"] for x in r["results"]] == ["1"]
    r = q(corpus, "longer than 10 minutes")
    assert [x["id"] for x in r["results"]] == ["4"]


def test_rrf_contribution_matches_rank(corpus):
    top = q(corpus, "pasta")["results"][0]
    assert top["rrf"]["keyword"]["rank"] == 1
    assert top["rrf"]["keyword"]["contrib"] == pytest.approx(1 / 61, abs=1e-5)
    names = {b["name"] for b in top["boosts"]}
    assert "recency" in names  # saved 2 days ago
    assert top["score"] == pytest.approx(sum(c["contrib"] for c in top["rrf"].values()) * (1 + sum(b["value"] for b in top["boosts"])), rel=1e-3)


def test_no_results_suggestions(corpus):
    r = q(corpus, "vintage mechanical watches")
    assert r["total"] == 0 and r["results"] == []
    texts = " ".join(b["text"] for b in r["suggestions"]["broaden"])
    assert "vintage" in texts and "0 saves" in texts
    r = q(corpus, "pastta")   # typo -> did you mean (vocab is stemmed)
    assert r["total"] == 0
    assert any("Did you mean" in b["text"] for b in r["suggestions"]["broaden"])


def test_facets_reflect_result_set(corpus):
    r = q(corpus, "lofi")
    creators = {f["value"] for f in r["facets"]["creator"]}
    assert creators == {"nomadnina", "lena"}


def test_facet_filter_applies(corpus):
    r = q(corpus, "", facets={"creator": ["lena"]})
    assert [x["id"] for x in r["results"]] == ["4"]
    assert any(w["method"] == "facet" for w in r["results"][0]["why"])
