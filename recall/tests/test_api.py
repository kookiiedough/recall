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
