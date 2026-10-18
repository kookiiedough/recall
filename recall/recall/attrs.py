"""Honest heuristic attributes. Each attribute stores the evidence that triggered it."""
from __future__ import annotations

import re

import numpy as np

RECIPE_WORDS = ["recipe", "ingredients", "tbsp", "tablespoon", "teaspoon", "tsp", "cups", "cup of", "oven", "bake",
                "preheat", "simmer", "boil", "grams", "minced", "garlic", "olive oil", "salt and pepper", "whisk",
                "stir", "sauté", "saute", "chop", "season", "degrees", "air fryer", "marinate"]
PRODUCT_PATTERNS = [r"link in (my )?bio", r"amazon", r"use (my )?code", r"discount code", r"#tiktokshop", r"#ad\b",
                    r"affiliate", r"shop (it|now|here)", r"https?://", r"\bon sale\b", r"#amazonfinds", r"product link",
                    r"\bshop\b.*\blink\b", r"#founditonamazon"]

ZS_LABELS = {
    "talking": "a person talking to the camera",
    "screen": "a screen recording of a phone app or website",
    "food": "food being cooked in a kitchen",
    "scenery": "a landscape or travel scenery",
    "workout": "a person exercising",
    "product": "a close-up of a product",
    "textcard": "text on a plain colored background",
}


def recipe(text: str) -> tuple[bool, str]:
    low = text.lower()
    hits = sorted({w for w in RECIPE_WORDS if w in low})
    return (len(hits) >= 2, f"recipe words: {', '.join(hits[:5])}" if hits else "")


def product_link(text: str) -> tuple[bool, str]:
    low = text.lower()
    hits = [p for p in PRODUCT_PATTERNS if re.search(p, low)]
    pretty = [re.sub(r"[\\()?]|\bb\b", "", h).replace("|", "/") for h in hits]
    return (bool(hits), f"matched: {', '.join(pretty[:3])}" if hits else "")


def zero_shot(frame_vecs: np.ndarray, label_vecs: np.ndarray) -> list[str]:
    if len(frame_vecs) == 0:
        return []
    logits = 100.0 * frame_vecs @ label_vecs.T
    keys = list(ZS_LABELS)
    return [keys[i] for i in logits.argmax(axis=1)]


def compute(text: str, duration: float | None, speech_seconds: float, frame_labels: list[str],
            ocr_lines_per_frame: float) -> dict:
    out: dict[str, dict] = {}
    v, ev = recipe(text)
    out["has_recipe"] = {"value": v, "evidence": ev}
    v, ev = product_link(text)
    out["has_product_link"] = {"value": v, "evidence": ev}
    n = len(frame_labels)
    talk_frac = frame_labels.count("talking") / n if n else 0.0
    screen_frac = frame_labels.count("screen") / n if n else 0.0
    speech_ratio = (speech_seconds / duration) if duration else 0.0
    th = n > 0 and speech_ratio >= 0.4 and talk_frac >= 0.5
    out["talking_head"] = {"value": th, "evidence": f"speech {speech_ratio:.0%} of runtime, CLIP 'person talking to camera' on {talk_frac:.0%} of keyframes" if n else "no keyframes"}
    sr = n > 0 and (screen_frac >= 0.5 or ocr_lines_per_frame >= 8)
    out["screen_recording"] = {"value": sr, "evidence": f"CLIP 'screen recording' on {screen_frac:.0%} of keyframes, {ocr_lines_per_frame:.1f} OCR lines/frame" if n else "no keyframes"}
    return out


def text_only(text: str) -> dict:
    """Attributes available after wave 1 (caption only)."""
    out = {}
    v, ev = recipe(text)
    out["has_recipe"] = {"value": v, "evidence": ev}
    v, ev = product_link(text)
    out["has_product_link"] = {"value": v, "evidence": ev}
    return out
