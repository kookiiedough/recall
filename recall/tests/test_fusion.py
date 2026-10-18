import math

from recall import fusion


def test_rrf_formula():
    s, c = fusion.rrf({"a": ["x", "y"], "b": ["y"]})
    assert math.isclose(s["x"], 1 / 61)
    assert math.isclose(s["y"], 1 / 62 + 1 / 61)
    assert c["y"]["a"]["rank"] == 2 and c["y"]["b"]["rank"] == 1


def test_agreement_beats_single_list_top():
    s, _ = fusion.rrf({"kw": ["a", "b"], "sem": ["b", "c"], "vis": ["c", "b"]})
    assert max(s, key=s.get) == "b"


def test_weights():
    s, _ = fusion.rrf({"kw": ["a"], "vis": ["b"]}, {"kw": 1.0, "vis": 0.5})
    assert s["a"] > s["b"] and math.isclose(s["b"], 0.5 / 61)


def test_boosts_are_small_and_monotonic():
    assert fusion.recency_boost(0) == 0.05
    assert fusion.recency_boost(90) == 0.025
    assert fusion.recency_boost(None) == 0.0
    assert fusion.affinity_boost(1, 10) == 0.0 and math.isclose(fusion.affinity_boost(10, 10), 0.05)
    s = fusion.apply_boosts({"a": 1.0, "b": 1.0}, {"a": {"recency": 0.05}})
    assert s["a"] == 1.05 and s["b"] == 1.0


def test_boost_breaks_near_ties_but_not_list_agreement():
    s, _ = fusion.rrf({"kw": ["a", "b"], "sem": ["a"]})          # a: in both lists, b: one list only
    final = fusion.apply_boosts(s, {"b": {"recency": 0.05, "creator_affinity": 0.05}})
    assert max(final, key=final.get) == "a"
    s, _ = fusion.rrf({"kw": ["a", "b"], "sem": ["b", "a"]})     # exact tie -> boost decides
    final = fusion.apply_boosts(s, {"b": {"recency": 0.01}})
    assert max(final, key=final.get) == "b"
