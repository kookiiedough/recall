"""Background ingestion worker: one thread, drains wave 1 then wave 2 in small batches."""
from __future__ import annotations

import logging
import threading
import time

from . import clusters, db, downloader, pipeline

log = logging.getLogger("recall.worker")


def default_provider(settings: dict):
    import os
    if os.environ.get("RECALL_FIXTURE"):
        from .fixture_provider import FixtureProvider
        return FixtureProvider(os.environ["RECALL_FIXTURE"],
                               fallback=pipeline.TikTokProvider(float(settings.get("oembed_rate", 1.0))))
    return pipeline.TikTokProvider(float(settings.get("oembed_rate", 1.0)))


class Worker:
    def __init__(self, provider_factory=None):
        self.provider_factory = provider_factory or default_provider
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "idle"
        self.last_error: str | None = None
        self.dirty_clusters = False
        self.last_cluster_n = 0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, name="recall-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set(); self._wake.set()
        if self.thread:
            self.thread.join(timeout=30)

    def kick(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        con = db.connect()
        provider = None
        breaker = downloader.Breaker(con)
        while not self._stop.is_set():
            try:
                s = db.get_settings(con)
                if s.get("worker_paused"):
                    self.state = "paused"
                    self._wake.wait(2); self._wake.clear(); continue
                if provider is None:
                    provider = self.provider_factory(s)
                self.state = "metadata"
                n1 = pipeline.wave1_batch(con, provider, s, limit=10, stop=self._stop.is_set)
                n2 = 0
                tripped = bool(s.get("deep_index", True) and s.get("video_downloads", True) and breaker.is_open())
                if n1 == 0 and s.get("deep_index", True) and not tripped:
                    # (when tripped, the circuit breaker holds the deep queue until the user presses Retry)
                    self.state = "deep-indexing"
                    n2 = pipeline.wave2_batch(con, provider, s, stop=self._stop.is_set, breaker=breaker)
                if n1 or n2:
                    self.dirty_clusters = True
                    continue
                live = con.execute("SELECT count(*) FROM videos WHERE status IN ('meta','partial','deep')").fetchone()[0]
                pending = con.execute("SELECT count(*) FROM videos WHERE status IN ('new') OR (status='meta' AND ?)",
                                      (1 if s.get("deep_index", True) and not tripped else 0,)).fetchone()[0]
                if self.dirty_clusters and pending == 0 and live >= 4:
                    self.state = "clustering"
                    clusters.recluster(con)
                    self.dirty_clusters = False
                    self.last_cluster_n = live
                self.state = "paused · downloads failing" if tripped else "idle"
                self._wake.wait(5); self._wake.clear()
            except Exception as e:  # never let the worker die
                log.exception("worker error")
                self.last_error = str(e)[:300]
                self.state = "error"
                time.sleep(5)
        con.close()
