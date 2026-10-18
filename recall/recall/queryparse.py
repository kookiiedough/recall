"""Rule-based query parser: pulls @creators, #hashtags, dates, durations and quoted/spoken phrases into
filters; whatever is left is the semantic intent."""
from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS.pop("may", None)
MONTHS["may"] = 5
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10, "a": 1, "an": 1}


@dataclass
class ParsedQuery:
    raw: str
    text: str = ""                      # semantic intent (filters removed)
    phrases: list[str] = field(default_factory=list)       # exact phrases (quoted)
    speech: list[str] = field(default_factory=list)        # "says X" -> transcript phrase
    creators: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    date_from: float | None = None
    date_to: float | None = None
    date_label: str | None = None
    dur_min: float | None = None
    dur_max: float | None = None
    dur_label: str | None = None

    def filters(self) -> list[dict]:
        out = []
        for c in self.creators:
            out.append({"type": "creator", "value": c, "label": f"@{c}"})
        for h in self.hashtags:
            out.append({"type": "hashtag", "value": h, "label": f"#{h}"})
        if self.date_label:
            out.append({"type": "date", "value": [self.date_from, self.date_to], "label": f"saved {self.date_label}"})
        if self.dur_label:
            out.append({"type": "duration", "value": [self.dur_min, self.dur_max], "label": self.dur_label})
        for s in self.speech:
            out.append({"type": "speech", "value": s, "label": f'says "{s}"'})
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["filters"] = self.filters()
        return d


def _start_of_day(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_range(year: int, month: int) -> tuple[float, float]:
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), month % 12 + 1, 1)
    return start.timestamp(), end.timestamp()


def _num(s: str) -> float:
    s = s.lower()
    return float(NUM_WORDS.get(s, s))


def parse(q: str, now: datetime | None = None) -> ParsedQuery:
    now = now or datetime.now()
    pq = ParsedQuery(raw=q)
    s = " " + q.strip() + " "

    def cut(m: re.Match) -> str:
        return " "

    # quoted phrases
    for m in re.finditer(r'"([^"]{2,})"|“([^”]{2,})”', s):
        pq.phrases.append((m.group(1) or m.group(2)).strip())
    # spoken phrase: "where a guy says stop doing this", "saying ..."
    m = re.search(r"\b(?:says|said|saying|say|mentions|mentioned|talks about)\s+(?:that\s+|like\s+)?[\"“]?([^\"”]+?)[\"”]?\s*$", s, re.I)
    if m:
        phrase = m.group(1).strip(" ,.")
        if len(phrase.split()) >= 1:
            pq.speech.append(phrase)
    # creators / hashtags
    pq.creators = [c.lower() for c in re.findall(r"(?<![\w@])@([\w.]{2,})", s)]
    s = re.sub(r"(?<![\w@])@[\w.]{2,}", " ", s)
    pq.hashtags = [h.lower() for h in re.findall(r"(?<![\w#])#([\w]+)", s)]
    s = re.sub(r"(?<![\w#])#\w+", " ", s)

    # ---- dates (applied to the *saved* date)
    today = _start_of_day(now)
    rules: list[tuple[str, callable]] = [
        (r"\b(?:saved\s+)?today\b", lambda m: (today, now + timedelta(seconds=1), "today")),
        (r"\b(?:saved\s+)?yesterday\b", lambda m: (today - timedelta(days=1), today, "yesterday")),
        (r"\b(?:saved\s+)?(?:in\s+the\s+)?(?:last|past)\s+(\d+|one|two|three|four|five|six|ten|a|an)\s+(day|week|month|year)s?\b",
         lambda m: (now - _delta(_num(m.group(1)), m.group(2)), now + timedelta(seconds=1), f"past {m.group(1)} {m.group(2)}s")),
        (r"\b(?:saved\s+)?(?:last|past|this)\s+week\b", lambda m: (now - timedelta(days=7), now + timedelta(seconds=1), "past 7 days")),
        (r"\b(?:saved\s+)?(?:last|past)\s+month\b", lambda m: (now - timedelta(days=30), now + timedelta(seconds=1), "past 30 days")),
        (r"\b(?:saved\s+)?this\s+month\b", lambda m: (today.replace(day=1), now + timedelta(seconds=1), "this month")),
        (r"\b(?:saved\s+)?(?:last|past)\s+year\b", lambda m: (now - timedelta(days=365), now + timedelta(seconds=1), "past year")),
        (r"\b(?:saved\s+)?this\s+year\b", lambda m: (datetime(now.year, 1, 1), now + timedelta(seconds=1), "this year")),
    ]
    for pat, fn in rules:
        m = re.search(pat, s, re.I)
        if m:
            a, b, label = fn(m)
            pq.date_from, pq.date_to, pq.date_label = a.timestamp(), b.timestamp(), label
            s = s[:m.start()] + " " + s[m.end():]
            break
    if pq.date_label is None:
        mon_pat = "|".join(sorted(MONTHS, key=len, reverse=True))
        m = re.search(rf"\b(?:saved\s+)?(?:in\s+|from\s+|during\s+)?({mon_pat})\.?(?:\s+(20\d\d))?\b", s, re.I)
        # avoid "may" used as a verb unless followed by a year or preceded by in/from
        if m and m.group(1).lower() == "may" and not (m.group(2) or re.search(r"(in|from|during)\s+may", m.group(0), re.I)):
            m = None
        if m:
            mon = MONTHS[m.group(1).lower()]
            year = int(m.group(2)) if m.group(2) else (now.year if mon <= now.month else now.year - 1)
            pq.date_from, pq.date_to = _month_range(year, mon)
            pq.date_label = f"{calendar.month_name[mon]} {year}"
            s = s[:m.start()] + " " + s[m.end():]
        else:
            m = re.search(r"\b(?:saved\s+)?(?:in\s+)?(20\d\d)\b", s)
            if m:
                y = int(m.group(1))
                pq.date_from, pq.date_to = datetime(y, 1, 1).timestamp(), datetime(y + 1, 1, 1).timestamp()
                pq.date_label = str(y)
                s = s[:m.start()] + " " + s[m.end():]

    # ---- durations
    unit = r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes)"

    def secs(n: str, u: str) -> float:
        return _num(n) * (60 if u.lower().startswith("m") else 1)

    dur_rules = [
        (rf"\b(?:under|less than|shorter than|below|<)\s*(\d+|one|two|three|five|ten|a)\s*{unit}\b",
         lambda m: (None, secs(m.group(1), m.group(2)), f"under {m.group(1)}{m.group(2)[0]}")),
        (rf"\b(?:over|more than|longer than|above|>)\s*(\d+|one|two|three|five|ten|a)\s*{unit}\b",
         lambda m: (secs(m.group(1), m.group(2)), None, f"over {m.group(1)}{m.group(2)[0]}")),
        (rf"\b(\d+)\s*-\s*(\d+)\s*{unit}\b",
         lambda m: (secs(m.group(1), m.group(3)), secs(m.group(2), m.group(3)), f"{m.group(1)}–{m.group(2)}{m.group(3)[0]}")),
        (r"\b(?:short|quick)\s+(?:videos?|clips?|ones?)\b|\bshort\b(?=\s*$)", lambda m: (None, 30.0, "under 30s")),
        (r"\b(?:long)\s+(?:videos?|clips?|ones?)\b", lambda m: (120.0, None, "over 2m")),
    ]
    for pat, fn in dur_rules:
        m = re.search(pat, s, re.I)
        if m:
            pq.dur_min, pq.dur_max, pq.dur_label = fn(m)
            s = s[:m.start()] + " " + s[m.end():]
            break

    s = re.sub(r'["“”]', " ", s)
    s = re.sub(r"\b(saved|from|in)\s*$", " ", s.strip(), flags=re.I)
    pq.text = re.sub(r"\s+", " ", s).strip(" ,.")
    return pq


def _delta(n: float, unit: str) -> timedelta:
    unit = unit.lower()
    return {"day": timedelta(days=n), "week": timedelta(weeks=n), "month": timedelta(days=30 * n),
            "year": timedelta(days=365 * n)}[unit]
