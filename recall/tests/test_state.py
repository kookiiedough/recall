import subprocess
from pathlib import Path

import pytest

from recall import db, ingest, importer, pipeline, wave1
from conftest import NO_MODELS


class FakeProvider:
    def __init__(self, video: Path | None = None, dead=(), flaky=(), fail_download=()):
        self.video, self.dead, self.fail_download = video, set(dead), set(fail_download)
        self.flaky = dict(flaky) if isinstance(flaky, dict) else dict.fromkeys(flaky, 1)
        self.meta_calls = 0

    def metadata(self, vid, url):
        self.meta_calls += 1
        if vid in self.dead:
            raise wave1.Dead("oEmbed HTTP 404")
        if self.flaky.get(vid):
            self.flaky[vid] -= 1
            raise wave1.Transient("HTTP 503")
        return {"caption": f"caption for {vid} #tag", "creator": "someone", "creator_name": "Some One",
                "hashtags": ["tag"], "thumb": None}

    def fetch_video(self, vid, url, tmp):
        if vid in self.fail_download:
            raise RuntimeError("network down")
        return self.video, {"track": "Song", "artist": "Artist", "duration": 2.0}, False


@pytest.fixture(scope="module")
def tiny_video(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "tiny.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x112:d=2", "-f", "lavfi",
                    "-i", "sine=f=440:d=2", "-shortest", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def _add(con, n=3):
    saves = [importer.Save(f"73000000000000000{i}", f"https://www.tiktok.com/@x/video/73000000000000000{i}", 1.7e9 + i) for i in range(n)]
    return ingest.add_saves(con, saves, "t", "export", resolve_short=False)


def test_transitions(con):
    _add(con, 1)
    vid = "730000000000000000"
    with pytest.raises(db.InvalidTransition):
        db.set_status(con, vid, "deep")          # new -> deep skips meta
    db.set_status(con, vid, "meta")
    db.set_status(con, vid, "deep")
    with pytest.raises(db.InvalidTransition):
        db.set_status(con, vid, "dead")          # deep can't die without a re-check
    with pytest.raises(db.InvalidTransition):
        db.set_status(con, vid, "bogus")


def test_reimport_only_adds_new(con):
    assert _add(con, 3)["added"] == 3
    r = _add(con, 5)
    assert r["added"] == 2 and r["already_indexed"] == 3
    assert con.execute("SELECT count(*) FROM videos").fetchone()[0] == 5


def test_deleted_items_stay_deleted_on_reimport(con):
    _add(con, 2)
    db.delete_video(con, "730000000000000000")
    assert _add(con, 2)["added"] == 0
    assert con.execute("SELECT count(*) FROM videos").fetchone()[0] == 1


def test_wave1_dead_and_transient_retry(con):
    _add(con, 3)
    prov = FakeProvider(dead={"730000000000000001"}, flaky={"730000000000000002"})
    pipeline.wave1_batch(con, prov, NO_MODELS)
    st = dict(con.execute("SELECT id, status FROM videos").fetchall())
    assert st == {"730000000000000000": "meta", "730000000000000001": "dead", "730000000000000002": "new"}
    pipeline.wave1_batch(con, prov, NO_MODELS)   # resume: only the transient one is retried
    st = dict(con.execute("SELECT id, status FROM videos").fetchall())
    assert st["730000000000000002"] == "meta" and prov.meta_calls == 4
    assert pipeline.wave1_batch(con, prov, NO_MODELS) == 0   # idempotent: nothing left to do


def test_transient_gives_up_as_failed(con):
    _add(con, 1)
    prov = FakeProvider(flaky={"730000000000000000": 99})
    for _ in range(pipeline.MAX_ATTEMPTS + 2):
        pipeline.wave1_batch(con, prov, NO_MODELS)
    assert con.execute("SELECT status FROM videos").fetchone()[0] == "failed"


def test_wave2_resume_after_failure_and_idempotent(con, tiny_video):
    _add(con, 2)
    good = FakeProvider(tiny_video)
    pipeline.wave1_batch(con, good, NO_MODELS)
    flaky = FakeProvider(tiny_video, fail_download={"730000000000000001"})
    assert pipeline.wave2_batch(con, flaky, NO_MODELS) == 1
    st = dict(con.execute("SELECT id, status FROM videos").fetchall())
    assert st == {"730000000000000000": "deep", "730000000000000001": "meta"}
    r = con.execute("SELECT stage, attempts, error FROM videos WHERE id='730000000000000001'").fetchone()
    assert r["stage"] is None and r["attempts"] == 1 and "network down" in r["error"]
    n_chunks = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert pipeline.wave2_batch(con, good, NO_MODELS) == 1         # resumes only the unfinished one
    assert pipeline.wave2_batch(con, good, NO_MODELS) == 0
    # re-running deep index for a video replaces (not duplicates) its rows
    db.set_status(con, "730000000000000000", "meta")
    pipeline.wave2_batch(con, good, NO_MODELS, ids=["730000000000000000"])
    per = dict(con.execute("SELECT video_id, count(*) FROM chunks GROUP BY video_id").fetchall())
    assert per["730000000000000000"] == per["730000000000000001"]
    assert con.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2 * per["730000000000000000"] >= n_chunks
    v = db.row_to_dict(con.execute("SELECT * FROM videos WHERE id='730000000000000000'").fetchone())
    assert v["sound"] == "Song — Artist" and v["duration"] and v["frames"]


def test_failed_downloads_end_in_failed(con, tiny_video):
    _add(con, 1)
    p = FakeProvider(tiny_video, fail_download={"730000000000000000"})
    pipeline.wave1_batch(con, p, NO_MODELS)
    for _ in range(pipeline.MAX_ATTEMPTS):
        pipeline.wave2_batch(con, p, NO_MODELS)
    assert con.execute("SELECT status FROM videos").fetchone()[0] == "failed"


def test_temp_video_is_deleted(con, tiny_video, tmp_path):
    import shutil
    from recall import config
    _add(con, 1)

    class TempProv(FakeProvider):
        def fetch_video(self, vid, url, tmp):
            dst = tmp / "dl.mp4"
            shutil.copy(self.video, dst)
            self.dst = dst
            return dst, {}, True
    p = TempProv(tiny_video)
    pipeline.wave1_batch(con, p, NO_MODELS)
    pipeline.wave2_batch(con, p, NO_MODELS)
    assert not p.dst.exists()                                     # video deleted
    assert list((config.media_dir() / "730000000000000000").glob("k_*.jpg"))   # keyframes kept
