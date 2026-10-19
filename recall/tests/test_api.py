import json

import pytest
from fastapi.testclient import TestClient

from recall import db


@pytest.fixture()
def client(home):
    from recall.server import create_app
    app = create_app(start_worker=False, warm=False)
    with TestClient(app) as c:
        db.set_settings(db.get(), {"text_embeddings": False, "visual": False})
        yield c


EXPORT = {"Activity": {"Favorite Videos": {"FavoriteVideoList": [
    {"Date": "2024-03-01 12:00:00", "Link": "https://www.tiktokv.com/share/video/7301234567890123456/"}]}}}


def test_import_queue_note_search_export_delete_purge(client):
    r = client.post("/api/import/file", files={"files": ("user_data.json", json.dumps(EXPORT), "application/json")})
    assert r.json()["results"][0]["added"] == 1
    r = client.post("/api/import/file", files={"files": ("user_data.json", json.dumps(EXPORT), "application/json")})
    assert r.json()["results"][0]["added"] == 0                       # idempotent re-import
    q = client.get("/api/queue").json()
    assert q["counts"] == {"new": 1}
    con = db.get()
    vid = "7301234567890123456"
    db.set_status(con, vid, "meta", caption="hello world", creator="me")
    assert client.post(f"/api/videos/{vid}/note", json={"note": "buy the green bowl"}).json()["ok"]
    res = client.get("/api/search", params={"q": "green bowl"}).json()
    assert res["results"][0]["id"] == vid and res["results"][0]["why"][0]["signal"] == "note"
    csv = client.post("/api/export", json={"format": "csv"})
    assert csv.status_code == 200 and vid in csv.text and csv.headers["x-recall-count"] == "1"
    js = client.post("/api/export", json={"format": "json"}).json()
    assert js["items"][0]["note"] == "buy the green bowl"
    st = client.get("/api/stats").json()
    assert st["live"] == 1 and st["coverage"]["Captions"]["pct"] == 100 and st["latency"]["n"] >= 1
    assert client.post("/api/purge", json={"confirm": "yes"}).status_code == 400     # typed confirmation required
    assert client.delete(f"/api/videos/{vid}").json()["ok"]
    assert client.get("/api/stats").json()["live"] == 0
    assert client.post("/api/purge", json={"confirm": "PURGE"}).json()["ok"]


def test_media_path_traversal_blocked(client):
    assert client.get("/media/../recall.db").status_code == 404
    assert client.get("/media/%2e%2e/recall.db").status_code == 404


def test_links_ingest(client):
    r = client.post("/api/import/links", json={"text": "look https://www.tiktok.com/@a/video/7301234567890123999 and this"}).json()
    assert r["added"] == 1


def test_breaker_banner_retry_and_settings(client):
    from recall import downloader
    client.post("/api/import/file", files={"files": ("user_data.json", json.dumps(EXPORT), "application/json")})
    con = db.get()
    vid = "7301234567890123456"
    db.set_status(con, vid, "meta", caption="hello world")
    from recall import pipeline
    pipeline.write_meta_chunks(con, vid, db.get_settings(con))
    db.set_status(con, vid, "partial", error="download failed [blocked]: Unexpected response",
                  signals={"degraded": True, "download_error": "blocked", "download_retry": 0}, attempts=3)
    b = downloader.Breaker(con)
    for i in range(5):
        b.failure("blocked", "Unexpected response from webpage request", str(i))
    q = client.get("/api/queue").json()
    assert q["breaker"]["open"] and q["breaker"]["kind"] == "blocked" and "cookies" in q["breaker"]["hint"]
    st = client.get("/api/stats").json()
    assert st["live"] == 1 and st["counts"]["partial"] == 1 and st["partial_reasons"] == {"blocked": 1}
    assert st["download_breaker"]["open"] and st["downloader"]["yt_dlp"]
    assert client.get("/api/search", params={"q": "hello"}).json()["results"][0]["status"] == "partial"
    r = client.post("/api/downloads/retry").json()
    assert r["requeued"] == 1 and not r["breaker"]["open"]
    assert con.execute("SELECT status, attempts FROM videos").fetchone()[:] == ("meta", 0)
    assert client.post("/api/settings", json={"cookies_browser": "netscape"}).status_code == 400
    s = client.post("/api/settings", json={"cookies_browser": "firefox", "ytdlp_impersonate": "never"}).json()
    assert s["cookies_browser"] == "firefox" and s["ytdlp_impersonate"] == "never"
    client.post("/api/downloads/thumbnail-only")
    assert client.get("/api/settings").json()["video_downloads"] is False
