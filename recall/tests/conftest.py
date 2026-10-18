import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    from recall import db
    db.reset_thread_connection()
    yield tmp_path / "home"
    db.reset_thread_connection()


@pytest.fixture()
def con(home):
    from recall import db
    c = db.connect()
    yield c
    c.close()


NO_MODELS = {"deep_index": True, "transcripts": False, "ocr": False, "visual": False, "text_embeddings": False,
             "sound": True, "frame_interval": 2.0, "max_keyframes": 4, "batch_size": 4, "oembed_rate": 0}
