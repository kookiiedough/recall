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
            time.sleep(1.6)
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
        b.close()
        print("console/page errors:", errors or "none")
        print("\n".join(str(x) for x in sorted(OUT.glob("*.png"))))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
