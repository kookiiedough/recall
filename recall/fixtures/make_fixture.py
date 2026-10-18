"""Generate a deterministic local fixture: synthetic short videos (ffmpeg colour scenes + on-screen
text + espeak-ng speech) and a matching fake TikTok export. Output: fixtures/out/ (not shipped).

Each video is designed so one signal uniquely identifies it (see `targets` in specs.json)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 540, 960

# (id suffix, creator, display, caption, sound, speech, scenes[(color, secs, overlay|None)], saved_days_ago | date str)
SPECS = [
    ("01", "chef_marta", "Marta Cooks", "Baked feta pasta, the viral one #pasta #feta #recipe", "Cooking Vibes — Marta Cooks",
     "Put the block of feta in the middle, add cherry tomatoes and olive oil, then bake for thirty minutes and stir in the pasta.",
     [("0x2E8B3A", 6, None), ("0x1F7A2E", 6, "BAKE 30 MIN")], 1),
    ("02", "sweetsbyjo", "Jo Bakes", "Grandma's lemon ricotta cake, finally nailed it #baking", "original sound — Jo Bakes",
     "This one took me three tries but it was worth every single attempt.",
     [("0xF2E6B8", 10, None)], 40),
    ("03", "techdad_mike", "Mike", "you need to hear this #techtips", "original sound — Mike",
     "Stop doing this. Stop leaving your phone on the charger all night. It slowly ruins the battery.",
     [("0x333333", 5, None), ("0x3A3A3A", 6, None)], 25),
    ("04", "nomadnina", "Nina Travels", "LA day two #travel #la", "Sunny Days — Lofi Beats",
     "We started really early in the morning to beat the crowds.",
     [("0xC9A27E", 4, None), ("0xC9A27E", 6, "SUNSET BOULEVARD WALKING TOUR")], 60),
    ("05", "fitwithkai", "Kai", "no equipment needed #fitness #workout", "Pump It — Gym Mix",
     "Thirty seconds on, ten seconds off. Keep your core tight and breathe.",
     [("0xFF8C00", 5, "5 MINUTE AB BURNER"), ("0xFF8C00", 5, None)], "2026-03-14 18:00:00"),
    ("06", "vanlifevera", "Vera", "waking up by the lake #vanlife #roadtrip", "Morning Light — Acoustic",
     "Coffee first, then we figure out where to drive today.",
     [("0x8B5A2B", 9, None)], 90),
    ("07", "stylebyana", "Ana", "GRWM for date night #grwm #outfit", "Date Night — Smooth RnB",
     "Getting ready with me, I am going with something simple and elegant tonight.",
     [("0x1E40FF", 6, None), ("0x1E40FF", 6, "BLUE SATIN DRESS")], 33),
    ("08", "lofi_lena", "Lena", "study with me, two hours #studytok", "Midnight Coffee — Lena Lofi",
     "Phones away. Let's focus together for the next hour.",
     [("0x2B2B48", 10, None)], 120),
    ("09", "chef_marta", "Marta Cooks", "Five ingredient tacos for busy weeknights #tacos #dinner", "Cooking Vibes — Marta Cooks",
     "Warm the tortillas in a dry pan, then add the beans, salsa and a little lime.",
     [("0xD62828", 10, None)], 15),
    ("10", "budgetbecca", "Becca", "how I saved my first ten thousand #money", "original sound — Becca",
     "Pay yourself first. Move the money the day your paycheck arrives.",
     [("0xFFFFFF", 10, "PAY YOURSELF FIRST")], 200),
    ("11", "gardengus", "Gus", "repotting my monstera #plants", "Repot Day — Garden Tunes",
     "Pick a pot only two inches wider than the old one.",
     [("0x6B4F2A", 9, None)], 75),
    ("12", "petsofpaul", "Paul", "he does this every morning #dogsoftiktok", "Silly Song — Pet Sounds",
     "Every morning at seven he brings me exactly one sock.",
     [("0x7A1FA2", 8, None)], 50),
    ("13", "quickclips", "QC", "tiny tip #lifehack", "Snap — Short Beats",
     "Freeze grapes to chill wine.",
     [("0xFFD60A", 5, None)], 45),
    ("14", "deskdiaries", "Dee", "my desk setup tour #desksetup", "Clean Desk — Lofi Desk Beats",
     "The monitor arm was the best upgrade I made this year.",
     [("0x111111", 10, None)], 100),
    ("15", "yogawithyara", "Yara", "morning stretch routine #yoga", "Breathe — Calm Piano",
     "Inhale as you reach up, exhale as you fold forward.",
     [("0x00A6A6", 10, None)], 130),
    ("16", "sweetsbyjo", "Jo Bakes", "Flaky buttermilk biscuits #baking #recipe", "original sound — Jo Bakes",
     "The secret is cold butter. Grate it frozen and work quickly.",
     [("0xEED9C4", 10, None)], 70),
    ("17", "techdad_mike", "Mike", "iPhone settings you should change #iphone", "original sound — Mike",
     "Turn off background app refresh for apps you rarely use.",
     [("0xDDDDDD", 10, "SETTINGS > GENERAL")], "2025-06-02 10:00:00"),
    ("18", "nomadnina", "Nina Travels", "best ramen in Tokyo #japan #travel", "Neon Streets — City Pop",
     "Line up before opening, the broth sells out by noon.",
     [("0xFF4FA3", 10, None)], 160),
]

TARGETS = [
    # (signal, query, expected suffix)
    ("caption", "lemon ricotta cake", "02"),
    ("caption", "five ingredient tacos", "09"),
    ("caption", "repotting monstera", "11"),
    ("transcript", "video where a guy says stop doing this", "03"),
    ("transcript", "the secret is cold butter", "16"),
    ("transcript", "brings me exactly one sock", "12"),
    ("transcript", "freeze grapes to chill wine", "13"),
    ("semantic", "how to keep my phone battery healthy", "03"),
    ("semantic", "putting money aside from my salary", "10"),
    ("ocr", "sunset boulevard", "04"),
    ("ocr", "ab burner", "05"),
    ("ocr", "pay yourself first", "10"),
    ("visual", "green", "01"),
    ("visual", "something purple", "12"),
    ("visual", "bright yellow", "13"),
    ("visual", "pink", "18"),
    ("creator", "@nomadnina ramen", "18"),
    ("creator", "vanlifevera", "06"),
    ("creator", "@budgetbecca", "10"),
    ("hashtag", "#vanlife", "06"),
    ("hashtag", "#desksetup", "14"),
    ("sound", "midnight coffee", "08"),
    ("sound", "neon streets city pop", "18"),
    ("date", "saved yesterday", "01"),
    ("date", "workout March", "05"),
    ("date", "iphone settings 2025", "17"),
    ("duration", "under 6 seconds", "13"),
    ("combined", "pasta recipe with feta in a green bowl", "01"),
    ("combined", "GRWM blue dress", "07"),
]

BASE = "74000000000000000"


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")


def esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(">", "\\>")


def make_video(out: Path, scenes, speech: str, work: Path) -> float:
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "speech.wav"
    run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(wav), speech])
    total = sum(s for _, s, _ in scenes)
    parts = []
    for i, (color, secs, text) in enumerate(scenes):
        p = work / f"scene{i}.mp4"
        vf = []
        if text:
            vf.append(f"drawtext=fontfile={FONT}:text='{esc(text)}':fontcolor=white:fontsize=40:box=1:boxcolor=black@0.75:"
                      f"boxborderw=18:x=(w-text_w)/2:y=h*0.42")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:r=24:d={secs}"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", str(p)]
        run(cmd)
        parts.append(p)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(wav),
         "-filter_complex", f"[1:a]adelay=500|500,apad,atrim=0:{total}[a]", "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-t", str(total), str(out)])
    return float(total)


def main(out_dir: str = "fixtures/out", now: datetime | None = None) -> Path:
    out = Path(out_dir).resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "videos").mkdir(parents=True)
    now = now or datetime.now()
    favs, meta = [], {}
    for suf, creator, display, caption, sound, speech, scenes, when in SPECS:
        vid = BASE + suf
        if isinstance(when, str):
            saved = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
        else:
            saved = (now - timedelta(days=when)).replace(hour=12, minute=0, second=0, microsecond=0)
        mp4 = out / "videos" / f"{vid}.mp4"
        dur = make_video(mp4, scenes, speech, out / "work" / vid)
        track, _, artist = sound.partition(" — ")
        favs.append({"Date": saved.strftime("%Y-%m-%d %H:%M:%S"), "Link": f"https://www.tiktokv.com/share/video/{vid}/"})
        meta[vid] = {"creator": creator, "creator_name": display, "caption": caption, "track": track, "artist": artist,
                     "duration": dur, "speech": speech, "video": str(mp4), "scenes": scenes}
    shutil.rmtree(out / "work")
    export = {"Activity": {"Favorite Videos": {"FavoriteVideoList": favs}, "Like List": {"ItemFavoriteList": []}}}
    (out / "user_data.json").write_text(json.dumps(export, indent=2))
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    (out / "targets.json").write_text(json.dumps([{"signal": s, "query": q, "expect": BASE + e} for s, q, e in TARGETS], indent=2))
    print(f"wrote {len(SPECS)} videos to {out}")
    return out


if __name__ == "__main__":
    main(*(sys.argv[1:2] or []))
