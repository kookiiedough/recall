# Recall — a private, searchable memory for your TikTok saves

Recall turns every TikTok you've saved ("Favorites") into a searchable document on **your own computer**.
Search by what was **said** (transcript), **shown** (visual similarity), **written on screen** (OCR),
the **caption**, **sound**, **creator** or **hashtag**, with filters like `last week`, `March`,
`@creator`, `#hashtag` and `under 30 seconds` understood straight from the command bar.
Every result shows *why it matched*: the actual signal, its raw score (BM25 / cosine), its rank in each
retrieval list, its reciprocal-rank-fusion contribution, and the timestamp inside the video.

Everything is free and open source, runs on the CPU, and loads one heavy model at a time so it fits an
8 GB laptop. The UI and API listen on `127.0.0.1` only. Nothing is uploaded anywhere.

---

## 1. Install (Windows / macOS / Linux, 8 GB RAM)

Requirements: **Python 3.10+** (developed and tested on 3.13 / Linux; the Windows and macOS scripts are provided
but were not tested on those OSes), **ffmpeg**, about **3 GB of disk** (≈2 GB Python packages incl. CPU PyTorch,
≈0.9 GB models downloaded on first use), and **≥ 2 GB of free RAM** while indexing.

| OS | ffmpeg | Python |
|---|---|---|
| Windows 10/11 | `winget install Gyan.FFmpeg` | `winget install Python.Python.3.12` |
| macOS | `brew install ffmpeg` | `brew install python@3.12` |
| Ubuntu/Debian | `sudo apt install ffmpeg python3-venv` | (system python3) |

Then, from this folder:

```bash
# macOS / Linux
./run.sh

# Windows (cmd or PowerShell)
run.bat
```

The first run creates `.venv`, installs **CPU-only** PyTorch (≈200 MB instead of the multi-GB CUDA build)
and the rest of `requirements.txt`, then opens `http://127.0.0.1:8765/`.

Manual install, if you prefer:

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
pip install -r requirements.txt
python -m recall                 # options: --port 8765 --home ~/.recall --no-browser --no-worker
```

Data lives in `~/.recall` (override with `--home` or `RECALL_HOME`): `recall.db` (SQLite), `media/`
(thumbnails + up to 8 small keyframes per video), `models/` (downloaded once from Hugging Face),
`exports/`. Delete that folder, or use **Settings → Purge Local Index**, to remove everything.

## 2. Get your TikTok data export

1. TikTok app → **Profile → ☰ → Settings and privacy → Account → Download your data**
   (web: tiktok.com → Settings → *Privacy* → *Download your data*).
2. Choose **JSON** as the format (TXT also works) and request *All data* (or at least *Activity*).
3. TikTok prepares the file, which takes anywhere from a few hours to a few days. Download the `.zip` from the same screen.
4. In Recall open **Import** and drop the `.zip` (or the `user_data*.json` / `Favorite Videos.txt` inside it).

Recall reads only your **Favorite Videos** (`Activity → Favorite Videos → FavoriteVideoList`, items
`{Date, Link}`). Key names drift between export versions, so the parser searches the whole tree for
favourite lists and accepts variants (`Your Activity`, `Favourite`, `date/link`, `VideoLink`, ...). Watch history
and likes are deliberately ignored. Re-importing a newer export only adds the new saves.

Other ways in: **Import → URL Ingest** (paste links, including `vm.tiktok.com` short links) and
**Import → Local Files** (drop MP4/MOV, or index a folder in place; your files are never moved or deleted).

## 3. How it works

```
export.json/.zip/.txt ─▶ videos table (status: new) ──▶ Wave 1: oEmbed ──▶ meta ──▶ Wave 2 (background) ──▶ deep
                                  │ dead links ◀────────────────┘                          │ failed (after 3 tries)
```

* **Storage**: one SQLite file. FTS5 (BM25, porter stemming) over caption / transcript / OCR / creator /
  sound / hashtag / note chunks; vectors are float16 BLOBs searched by numpy brute force (fast enough
  for thousands of saves; sqlite-vec was not needed). Each video row carries a status state machine
  (`new → meta → deep`, plus `dead` / `failed`), enforced in `recall/db.py`. Jobs are keyed by TikTok video id,
  batched, idempotent (each step *replaces* that video's rows) and resumable: anything not `deep` is picked up again.
* **Wave 1 (fast)**: `https://www.tiktok.com/oembed?url=` gives caption, creator and thumbnail
  (rate-limited to 1 request/s; 400/403/404 means dead). Hashtags are parsed from the caption. The thumbnail is stored locally.
* **Wave 2 (deep, background, one batch of 4 at a time)**: yt-dlp downloads a small rendition into a temp
  folder (and supplies sound/track, duration, hashtags). Then the models run *sequentially over the batch*, each
  loaded and then released: faster-whisper (int8, word timestamps), then RapidOCR on one frame every 2 s
  (repeated text deduped), then CLIP ViT-B/32 on up to 8 keyframes plus the thumbnail, then MiniLM-L6 text embeddings.
  Keyframes and thumbnail are kept; **the video file is deleted**.
* **Search**: a rule-based parser pulls `@creator`, `#hashtag`, dates (`today`, `yesterday`, `last week`,
  `past 3 days`, `March`, `March 2024`, `2025`), durations (`under 30s`, `over 2 min`, `short videos`), quoted
  phrases and "says …" speech phrases into filters. The rest is the semantic intent. Three retrieval lists
  (BM25 keyword, MiniLM semantic over chunks with timestamps, CLIP text→keyframe), plus an exact-phrase list,
  are fused with **RRF (k=60)**, then given small boosts (≤ +5 % recency, ≤ +5 % creator affinity).
  A semantic or visual hit has to clear a threshold (MiniLM cos ≥ 0.36, or ≥ 0.50 for 1–2 word queries;
  CLIP cos ≥ 0.235 *and* z ≥ 1.5 vs. the corpus) so that "no results" can genuinely happen. The empty state then
  shows the nearest below-threshold matches with their scores, terms that occur in 0 saves, did-you-mean
  spellings from the FTS vocabulary, and real index status.
* **Facets** (computed over the current result set): cluster, attributes, creator, sound, hashtag, saved date, duration.
* **Attributes are heuristics, each storing its evidence**: *has recipe* (≥ 2 recipe words in caption/transcript/OCR);
  *has product link* (link-in-bio / amazon / discount code / URL patterns); *talking head* (speech ≥ 40 % of the runtime
  **and** CLIP zero-shot "person talking to the camera" on ≥ 50 % of keyframes); *screen recording* (CLIP
  zero-shot "screen recording" on ≥ 50 % of keyframes, or ≥ 8 OCR lines per frame).
* **Clusters**: KMeans over each save's mean MiniLM vector (plus 0.5× its mean CLIP vector), k ≈ √n. Clusters are named
  with a category (Recipes, Fitness, Travel, …) only when ≥ 40 % of members contain that category's words; otherwise
  they get their top c-TF-IDF keywords and hashtags. Renames and pins survive re-clustering (members matched by Jaccard overlap).

## 4. Settings and their per-video cost

Measured on the build machine (8 vCPU, CPU only, 4 threads). The Settings screen shows the averages
**measured on your own machine**.

| Setting | What it does | Cost / video (real TikToks, 10–96 s, avg of 13) | Cost (18 synthetic 5–12 s clips) |
|---|---|---|---|
| Wave 1 metadata (always) | oEmbed + thumbnail | 1.4 s (mostly the 1 req/s rate limit) | — |
| Deep indexing | yt-dlp download + ffmpeg frames/audio | 4.9 s (network-bound) | 0.25 s |
| Transcripts | faster-whisper **base** int8 (tiny is faster) | 1.5 s | 0.6 s |
| OCR | RapidOCR, 1 frame / 2 s | 5.5 s (the most expensive; scales with length) | 1.0 s |
| Visual similarity | CLIP ViT-B/32, ≤ 8 keyframes + thumbnail | 0.2 s | 0.1 s |
| Semantic text | MiniLM-L6 on every chunk | < 0.1 s | < 0.1 s |
| Sound | metadata only | free | free |
| **Total wave 2** | | **11.7 s/video** (≈ 3.3 h per 1,000 saves) | 2.0 s |

Memory: peak resident memory of the whole process (server + worker + all models, loaded one heavy model
at a time) was **≈ 1.8–1.9 GB** in every run. Search takes **≈ 20–60 ms** server-side once the two query
encoders are warm (they are preloaded at startup, ≈ 10 s). Disk: about 0.2 MB per save (DB, thumbnail, keyframes).
To lower RAM or CPU: choose Whisper `tiny`, turn OCR off (the biggest CPU cost), or set batch size 1.

## 5. Known limits — please read

* **yt-dlp downloading is against TikTok's Terms of Service** and can break whenever TikTok changes its site.
  Deep indexing is for **personal use only**, on videos you saved yourself. Turn *Deep Indexing* off in
  Settings to use only TikTok's official oEmbed endpoint (caption / creator / thumbnail search still works).
* Photo-carousel posts have no video frames, so they get a transcript of the audio but no OCR or visual signal (seen in testing).
* Private, deleted or region-blocked videos are marked **dead** (oEmbed 400/404). They stay out of results and can be retried.
* **iOS share-sheet capture is not supported**. A **browser extension** for one-click saving is planned for v2.
* **MP4 clip export is not implemented** (videos are deleted after indexing). JSON and CSV export work.
* **Encrypted storage (SQLCipher) is not included**: its Python bindings need a native build on Windows. Use
  BitLocker / FileVault / LUKS full-disk encryption instead. The Settings toggle is shown disabled for that reason.
* Attributes and cluster names are heuristics. Each attribute records the evidence behind it, so check it before relying on it.
* Thresholds for "semantic" and "visual" hits were calibrated on small test sets. Very short or vague queries can over- or under-match.
* The first launch downloads ≈ 0.9 GB of models from Hugging Face. After that, Recall needs the network only for TikTok.

## 6. Development & tests

```bash
pip install -r requirements-dev.txt && python -m playwright install chromium
python -m pytest -q tests                         # unit + API tests (no models needed)
python fixtures/make_fixture.py                   # 18 synthetic videos (ffmpeg + espeak-ng) + fake export
python scripts/e2e_fixture.py                     # full pipeline on the fixture; writes tests/e2e_report.json
python scripts/real_import.py fixtures/real/real_links_export.json "baked feta pasta"   # needs network
RECALL_FIXTURE=fixtures/out python -m recall --no-browser &   # app with offline fixture provider
python scripts/screenshots.py http://127.0.0.1:8765 fixtures/out/user_data.json
```

`RECALL_FIXTURE` swaps **only** the two network calls (oEmbed + download) for local files. Everything after that
(ffmpeg, Whisper, OCR, CLIP, MiniLM, FTS, fusion) is the real pipeline.

Layout: `recall/` (`db.py` schema + state machine, `importer.py` export parsing, `pipeline.py` waves 1/2,
`models.py` model budget, `queryparse.py`, `search.py`, `fusion.py`, `clusters.py`, `attrs.py`, `server.py`,
`worker.py`, `static/` UI), `tests/`, `fixtures/`, `scripts/`, `screenshots/`.
