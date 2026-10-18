"""`python -m recall` — start the local server + background worker and open the UI."""
from __future__ import annotations

import argparse
import logging
import os
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(prog="recall", description="Recall — private memory for your TikTok saves")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("RECALL_PORT", 8765)))
    ap.add_argument("--home", help="data directory (default ~/.recall or $RECALL_HOME)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-worker", action="store_true", help="serve search only, don't process the queue")
    a = ap.parse_args()
    if a.home:
        os.environ["RECALL_HOME"] = a.home
    from . import config
    config.models_dir()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    import uvicorn
    from .server import create_app
    app = create_app(start_worker=not a.no_worker)
    url = f"http://{a.host}:{a.port}/"
    print(f"\n  Recall is running at {url}  (data: {config.home()})\n")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
