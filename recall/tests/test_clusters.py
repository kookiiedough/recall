import numpy as np

from recall import clusters, db, pipeline
from conftest import NO_MODELS


def _seed(con):
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=384), rng.normal(size=384)
    for i in range(12):
        vid = f"v{i}"
        food = i < 6
        cap = "creamy pasta recipe #recipe #pasta" if food else "leg day workout #fitness #gym"
        db.upsert_new_video(con, vid, None, 1.7e9 + i)
        db.set_status(con, vid, "meta", caption=cap, hashtags=["recipe", "pasta"] if food else ["fitness", "gym"])
        pipeline.write_meta_chunks(con, vid, NO_MODELS)
        v = (a if food else b) + rng.normal(scale=0.1, size=384)
        v = (v / np.linalg.norm(v)).astype(np.float16)
        con.execute("INSERT INTO vectors(video_id, space, chunk_id, t, vec) VALUES (?,?,?,?,?)", (vid, "text", None, None, v.tobytes()))
    con.commit()


def test_clusters_named_from_keywords_and_survive_recluster(con):
    _seed(con)
    clusters.recluster(con, k=2)
    cl = {c["name"]: c for c in clusters.list_clusters(con)}
    assert set(cl) == {"Recipes", "Fitness"}
    assert all(c["size"] == 6 for c in cl.values())
    rid = cl["Recipes"]["id"]
    con.execute("UPDATE clusters SET custom_name='Dinner ideas', pinned=1 WHERE id=?", (rid,))
    con.commit()
    clusters.recluster(con, k=2)
    after = {c["id"]: c for c in clusters.list_clusters(con)}
    assert after[rid]["name"] == "Dinner ideas" and after[rid]["pinned"] == 1 and after[rid]["size"] == 6
    assert clusters.list_clusters(con)[0]["id"] == rid   # pinned first


def test_unique_names():
    assert clusters.name_cluster(["pasta", "feta"], ["recipe"], [{"pasta"}, {"cake", "bake"}]) == "Recipes"
    assert clusters.name_cluster(["lamp", "sofa"], [], [{"lamp"}, {"sofa"}]) == "Lamp · Sofa"
