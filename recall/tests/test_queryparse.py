from datetime import datetime

from recall.queryparse import parse

NOW = datetime(2026, 10, 18, 15, 0, 0)


def test_plain_text_is_intent():
    p = parse("pasta recipe with feta in a green bowl", now=NOW)
    assert p.text == "pasta recipe with feta in a green bowl" and p.filters() == []


def test_creator_and_hashtag():
    p = parse("@Chef_Marta pasta #Feta", now=NOW)
    assert p.creators == ["chef_marta"] and p.hashtags == ["feta"] and p.text == "pasta"


def test_email_is_not_creator():
    assert parse("mail me at a@b.com", now=NOW).creators == []


def test_last_week_and_yesterday():
    p = parse("grwm last week", now=NOW)
    assert p.text == "grwm" and p.date_label == "past 7 days"
    assert abs(p.date_from - (NOW.timestamp() - 7 * 86400)) < 1
    y = parse("saved yesterday", now=NOW)
    assert y.date_from == datetime(2026, 10, 17).timestamp() and y.date_to == datetime(2026, 10, 18).timestamp()
    assert y.text == ""


def test_past_n_days():
    p = parse("tacos past 3 days", now=NOW)
    assert p.text == "tacos" and abs(p.date_from - (NOW.timestamp() - 3 * 86400)) < 1


def test_month_resolves_to_most_recent():
    p = parse("workout March", now=NOW)
    assert p.date_label == "March 2026" and p.text == "workout"
    p = parse("soup in December", now=NOW)
    assert p.date_label == "December 2025"
    p = parse("soup march 2024", now=NOW)
    assert p.date_from == datetime(2024, 3, 1).timestamp() and p.date_to == datetime(2024, 4, 1).timestamp()


def test_may_as_verb_is_not_a_date():
    assert parse("what may happen", now=NOW).date_label is None
    assert parse("recipes from may", now=NOW).date_label == "May 2026"


def test_year():
    p = parse("iphone settings 2025", now=NOW)
    assert p.date_label == "2025" and p.text == "iphone settings"


def test_durations():
    p = parse("workout under 30 seconds", now=NOW)
    assert (p.dur_min, p.dur_max) == (None, 30.0) and p.text == "workout"
    p = parse("recipe longer than 2 minutes", now=NOW)
    assert (p.dur_min, p.dur_max) == (120.0, None)
    p = parse("short videos about dogs", now=NOW)
    assert p.dur_max == 30.0 and "dogs" in p.text


def test_speech_and_quoted_phrases():
    p = parse("video where a guy says stop doing this", now=NOW)
    assert p.speech == ["stop doing this"]
    q = parse('"link in bio" skincare', now=NOW)
    assert q.phrases == ["link in bio"] and "skincare" in q.text


def test_combined_filters():
    p = parse("@nomadnina #travel ramen last month under 60s", now=NOW)
    types = sorted(f["type"] for f in p.filters())
    assert types == ["creator", "date", "duration", "hashtag"] and p.text == "ramen"
