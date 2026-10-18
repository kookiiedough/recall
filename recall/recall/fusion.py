"""Reciprocal rank fusion + boosts, kept pure so it can be unit-tested."""
from __future__ import annotations

import math

RRF_K = 60


def rrf(ranked_lists: dict[str, list[str]], weights: dict[str, float] | None = None, k: int = RRF_K
        ) -> tuple[dict[str, float], dict[str, dict[str, dict]]]:
    """ranked_lists: name -> [doc ids best first]. Returns (scores, contributions[doc][list] = {rank, contrib})."""
    weights = weights or {}
    scores: dict[str, float] = {}
    contrib: dict[str, dict[str, dict]] = {}
    for name, ids in ranked_lists.items():
        w = weights.get(name, 1.0)
        for r, d in enumerate(ids, start=1):
            c = w / (k + r)
            scores[d] = scores.get(d, 0.0) + c
            contrib.setdefault(d, {})[name] = {"rank": r, "contrib": c}
    return scores, contrib


def recency_boost(age_days: float | None, strength: float = 0.05, half_life: float = 90.0) -> float:
    if age_days is None:
        return 0.0
    return strength * math.exp(-max(0.0, age_days) * math.log(2) / half_life)


def affinity_boost(creator_count: int, max_count: int, strength: float = 0.05) -> float:
    if max_count <= 1 or creator_count <= 1:
        return 0.0
    return strength * math.log(creator_count) / math.log(max_count)


def apply_boosts(scores: dict[str, float], boosts: dict[str, dict[str, float]]) -> dict[str, float]:
    """boosts[doc] = {name: fraction}; final = rrf * (1 + sum(fractions))."""
    return {d: s * (1.0 + sum(boosts.get(d, {}).values())) for d, s in scores.items()}
