from __future__ import annotations

import re

HASHTAG_RE = re.compile(r"#([\w\u00C0-\uFFFF]+)", re.UNICODE)

STOPWORDS = set("""a an the and or of to in on at for with from by is are was were be been it its this that these those
i me my we our you your he she they them his her their what which who whom where when why how video videos tiktok
tiktoks clip one some any all about into over under as so just very really like get got guy girl someone says said
saying say find show me there here do does did not no yes can could would should will shall
something anything thing things stuff kind sort
""".split())


def hashtags(text: str | None) -> list[str]:
    seen, out = set(), []
    for h in HASHTAG_RE.findall(text or ""):
        h = h.lower()
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w']+", (text or "").lower()) if t]


def content_terms(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOPWORDS and len(t) > 1]


def fmt_time(t: float | None) -> str:
    if t is None:
        return ""
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{int(t % 60):02d}"
