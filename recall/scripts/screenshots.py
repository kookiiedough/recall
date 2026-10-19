"""Drive the running app with headless Chromium and capture every screen at 1440x900.
Usage: python scripts/screenshots.py http://127.0.0.1:8765 fixtures/out/user_data.json"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "screenshots"


def get(base, path):
    return json.loads(urllib.request.urlopen(base + path).read())


def main(base: str, export: str | None) -> None:
    """Needs RECALL_HOME + RECALL_FIXTURE set to the same values as the running server."""
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(base + "/#/import")
        pg.wait_for_selector(".queue-item")
        if export:
            pg.set_input_files("#file-input", export)
            # wait until the worker is visibly deep-indexing something
            for _ in range(240):
                q = get(base, "/api/queue")
                if any(a["status"] == "meta" and a["stage"] in ("transcribing", "reading on-screen text", "visual embedding")
                       for a in q["active"]) and q["counts"].get("deep", 0) > 0:
                    break
                time.sleep(0.5)
            time.sleep(0.2)
        pg.screenshot(path=str(OUT / "03_import_queue.png"))
        # wait for the queue to drain and clusters to be built
        for _ in range(900):
            q = get(base, "/api/queue")
            if not q["counts"].get("new") and not q["counts"].get("meta") and q["worker"] == "idle":
                break
            time.sleep(1)
        time.sleep(1)
        pg.goto(base + "/#/")
        pg.wait_for_selector(".big-number")
        pg.wait_for_timeout(500)
        pg.screenshot(path=str(OUT / "01_home.png"))
        for name, query in [("02_results", "pasta recipe with feta in a green bowl"),
                            ("02b_results_transcript", "video where a guy says stop doing this"),
                            ("02c_results_visual", "green"),
                            ("04_no_results", "vintage mechanical watches from the 1940s in silver")]:
            pg.goto(base + "/#/")
            pg.wait_for_selector("#cmd")
            pg.type("#cmd", query, delay=15)  # debounced live search navigates to results
            pg.wait_for_selector(".video-doc, .empty-hero", timeout=20000)
            pg.wait_for_timeout(1200)
            pg.screenshot(path=str(OUT / f"{name}.png"))
        pg.screenshot(path=str(OUT / "04_no_results_full.png"), full_page=True)
        pg.goto(base + "/#/search?q=" + urllib.request.quote("pasta recipe with feta in a green bowl"))
        pg.wait_for_selector(".video-doc"); pg.wait_for_timeout(1000)
        pg.screenshot(path=str(OUT / "02_results_full.png"), full_page=True)
        for name, route in [("05_clusters", "#/clusters"), ("06_export", "#/export"), ("07_settings", "#/settings")]:
            pg.goto(base + "/" + route)
            pg.wait_for_selector(".page-title, h1, .cluster-row")
            pg.wait_for_timeout(800)
            pg.screenshot(path=str(OUT / f"{name}.png"))
        pg.screenshot(path=str(OUT / "07_settings_full.png"), full_page=True)
        pg.goto(base + "/#/settings?sec=downloads")
        pg.wait_for_selector("#sec-downloads"); pg.wait_for_timeout(800)
        pg.screenshot(path=str(OUT / "07b_settings_downloads.png"))
        # Circuit-breaker banner. The fixture can't make TikTok block us on demand, so this state is SIMULATED:
        # 5 consecutive 'blocked' failures are recorded in the breaker and two saves are run through the real
        # thumbnail-only (degraded) path with a provider that raises TikTok's exact error.
        sys.path.insert(0, str(ROOT))
        from recall import db, downloader, pipeline
        from recall.fixture_provider import FixtureProvider
        con = db.connect()
        ids = [r[0] for r in con.execute("SELECT id FROM videos WHERE status='deep' ORDER BY saved_at DESC LIMIT 2")]

        class Blocked(FixtureProvider):
            def fetch_video(self, vid, url, tmp):
                raise downloader.DownloadError("blocked", "Unexpected response from webpage request")
        post = lambda path, body: urllib.request.urlopen(urllib.request.Request(
            base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))
        post("/api/worker", {"paused": True}); time.sleep(3)
        for vid in ids:
            db.set_status(con, vid, "meta")
        pipeline.wave2_batch(con, Blocked(ROOT / "fixtures" / "out"), db.get_settings(con), ids=ids)
        br = downloader.Breaker(con)
        for i in range(downloader.BREAKER_THRESHOLD):
            br.failure("blocked", "Unexpected response from webpage request", ids[i % len(ids)])
        post("/api/worker", {"paused": False})
        cap = con.execute("SELECT caption FROM videos WHERE id=?", (ids[0],)).fetchone()[0]
        for name, route in [("08_home_breaker_banner", "#/"), ("09_import_breaker_banner", "#/import")]:
            pg.goto(base + "/" + route)
            pg.wait_for_selector("#breaker"); pg.wait_for_timeout(1200)
            pg.screenshot(path=str(OUT / f"{name}.png"))
        pg.goto(base + "/#/search?q=" + urllib.request.quote(" ".join(cap.split()[:3])))
        pg.wait_for_selector(".video-doc"); pg.wait_for_timeout(1000)
        pg.screenshot(path=str(OUT / "10_results_thumbnail_only.png"))
        # the banner's Retry button really closes the breaker and re-queues
        pg.goto(base + "/#/import"); pg.wait_for_selector("#breaker")
        pg.click("[data-breaker=retry]"); pg.wait_for_timeout(2500)
        assert not get(base, "/api/queue")["breaker"]["open"], "Retry did not close the breaker"
        con.close()
        b.close()
        print("console/page errors:", errors or "none")
        print("\n".join(str(x) for x in sorted(OUT.glob("*.png"))))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
