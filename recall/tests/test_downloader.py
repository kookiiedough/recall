"""yt-dlp hardening: error classification, concise logging, request profiles, cookies, circuit breaker."""
from pathlib import Path

import pytest

from recall import db, downloader, importer, ingest, pipeline
from conftest import NO_MODELS

UNEXPECTED = ("ERROR: [TikTok] 7287722526472490272: Unexpected response from webpage request; please report this "
              "issue on  https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template. "
              "Confirm you are on the latest version using  yt-dlp -U")


@pytest.mark.parametrize("msg,kind", [
    (UNEXPECTED, "blocked"),
    ("ERROR: [TikTok] 1: Unable to download webpage: HTTP Error 403: Forbidden (caused by <HTTPError 403>)", "forbidden"),
    ("HTTP Error 429: Too Many Requests", "rate_limited"),
    ("could not find chrome cookies database in \"C:\\Users\\x\"", "cookies"),
    ("Failed to decrypt with DPAPI. See https://github.com/yt-dlp/yt-dlp/issues/10927", "cookies"),
    ("[TikTok] 1: This video is private", "unavailable"),
    ("[TikTok] 1: Unable to extract universal data for rehydration", "extract_failed"),
    ("<urlopen error [Errno 11001] getaddrinfo failed>", "network"),
    ("something odd", "other"),
])
def test_classify(msg, kind):
    assert downloader.classify(msg) == kind


def test_concise_strips_report_boilerplate():
    assert downloader.concise(UNEXPECTED) == "Unexpected response from webpage request"


def test_profiles_and_cookies(monkeypatch):
    monkeypatch.setattr(downloader, "impersonation_available", lambda: True)
    names = [n for n, _ in downloader.profiles({"ytdlp_impersonate": "auto"})]
    assert names == ["desktop-ua", "impersonate", "yt-dlp-default"]
    assert [n for n, _ in downloader.profiles({"ytdlp_impersonate": "always"})][0] == "impersonate"
    assert "impersonate" not in [n for n, _ in downloader.profiles({"ytdlp_impersonate": "never"})]
    ua = dict(downloader.profiles({"ytdlp_user_agent": "MyUA/1"}))["desktop-ua"]["http_headers"]["User-Agent"]
    assert ua == "MyUA/1"
    monkeypatch.setattr(downloader, "impersonation_available", lambda: False)
    assert [n for n, _ in downloader.profiles({})] == ["desktop-ua", "yt-dlp-default"]
    assert downloader.cookie_opts({"cookies_browser": "firefox"}) == {"cookiesfrombrowser": ("firefox", None, None, None)}
    assert downloader.cookie_opts({"cookies_browser": "none"}) == {}
    assert "cookiefile" in downloader.cookie_opts({"cookies_browser": "chrome", "cookies_file": "~/c.txt"})


def test_download_rotates_profiles_quietly(monkeypatch, tmp_path, capsys):
    """Each profile gets one go; yt-dlp's own error printing is swallowed; the winner becomes sticky."""
    import yt_dlp
    calls = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def extract_info(self, url, download):
            name = "ua" if "http_headers" in self.opts else "default"
            calls.append((name, self.opts.get("cookiesfrombrowser")))
            if name == "ua":
                self.opts["logger"].error(UNEXPECTED)        # what yt-dlp would print to stderr
                raise yt_dlp.utils.DownloadError(UNEXPECTED)
            (tmp_path / "1.mp4").write_bytes(b"x")
            return {"id": "1", "ext": "mp4"}
        def prepare_filename(self, info):
            return str(tmp_path / "1.mp4")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(downloader, "impersonation_available", lambda: False)
    downloader._preferred.clear()
    waits = []
    path, meta, prof = downloader.download("u", tmp_path, {"cookies_browser": "edge"}, lambda s: waits.append(s) or False)
    assert prof == "yt-dlp-default" and path.exists() and waits == [2.0]
    assert calls == [("ua", ("edge", None, None, None)), ("default", ("edge", None, None, None))]
    calls.clear()
    downloader.download("u", tmp_path, {}, lambda s: False)
    assert calls[0][0] == "default"                                # sticky winner tried first
    out = capsys.readouterr()
    assert "Unexpected response" not in out.out + out.err


def test_download_raises_classified_error(monkeypatch, tmp_path):
    import yt_dlp

    class Boom:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def extract_info(self, *a, **k):
            raise yt_dlp.utils.DownloadError("ERROR: could not find firefox cookies database in /x")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Boom)
    with pytest.raises(downloader.DownloadError) as e:
        downloader.download("u", tmp_path, {"cookies_browser": "firefox"}, lambda s: False)
    assert e.value.kind == "cookies"


def test_breaker_opens_after_five_same_errors(con):
    b = downloader.Breaker(con)
    for i in range(4):
        b.failure("blocked", "Unexpected response", str(i))
    assert not b.is_open() and b.backoff_s() == 40.0
    b.failure("forbidden", "403", "x")                 # different error restarts the count
    assert b.state()["consecutive"] == 1
    for i in range(4):
        b.failure("forbidden", "403", str(i))
    assert b.is_open() and b.public()["hint"]
    b.reset()
    assert not b.is_open() and b.backoff_s() == 0
    b.failure("network", "timed out", "n")             # network blips don't count
    assert b.state()["consecutive"] == 0
    b.failure("cookies", "could not find chrome cookies", "c")
    assert b.is_open()                                 # a config error opens immediately
    b.success()
    assert not b.is_open()


class BlockedProvider:
    def __init__(self, video=None):
        self.n = 0
        self.video = video

    def metadata(self, vid, url):
        return {"caption": f"caption {vid}", "creator": "c", "creator_name": "C", "hashtags": [], "thumb": None}

    def fetch_video(self, vid, url, tmp):
        self.n += 1
        raise downloader.DownloadError("blocked", "Unexpected response from webpage request")


def test_pipeline_trips_breaker_and_keeps_rest_queued(con, monkeypatch):
    saves = [importer.Save(f"74000000000000000{i}", f"https://www.tiktok.com/@x/video/74000000000000000{i}", 1.7e9 + i)
             for i in range(8)]
    ingest.add_saves(con, saves, "t", "export", resolve_short=False)
    p = BlockedProvider()
    br = downloader.Breaker(con)
    monkeypatch.setattr(br, "backoff_s", lambda: 0.0)
    pipeline.wave1_batch(con, p, NO_MODELS)
    s = {**NO_MODELS, "batch_size": 8}
    done = pipeline.wave2_batch(con, p, s, breaker=br)
    st = dict(con.execute("SELECT status, count(*) FROM videos GROUP BY status").fetchall())
    assert p.n == 5 and done == 5 and br.is_open()
    assert st == {"partial": 5, "meta": 3}
    assert pipeline.wave2_batch(con, p, s, breaker=br) == 0 and p.n == 5     # paused: no more requests
    assert con.execute("SELECT count(*) FROM videos WHERE stage IS NOT NULL").fetchone()[0] == 0
    err = con.execute("SELECT error FROM videos WHERE status='partial' LIMIT 1").fetchone()[0]
    assert err.startswith("download failed [blocked]: Unexpected response")
