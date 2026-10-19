"""yt-dlp wrapper for wave 2: request profiles, browser cookies, quiet logging, error classes, circuit breaker.

Why profiles: since Aug 2026 TikTok answers some yt-dlp requests with a block page instead of the video page
("Unexpected response from webpage request", or a plain HTTP 403). What gets through depends on the
User-Agent / TLS fingerprint and on how hard the IP has been hitting TikTok (see README "Downloads failing").
No single setting works everywhere, so Recall tries a short list of request profiles per video, remembers
the one that last worked, and backs off / stops instead of hammering TikTok.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("recall.download")

# A desktop Chrome UA outside the 140-149 range that TikTok's bot heuristic rejected in Aug-Sep 2026
# (yt-dlp issues #17403, #17604). Users can override it in Settings.
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/139.0.0.0 Safari/537.36")
BROWSERS = ("chrome", "edge", "firefox", "safari", "brave")
BREAKER_THRESHOLD = 5


# ------------------------------------------------------------------ environment
def ytdlp_version() -> str | None:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return None


def impersonation_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: F401
        return True
    except Exception:
        return False


def environment() -> dict:
    cc = None
    try:
        from importlib.metadata import version
        cc = version("curl_cffi")
    except Exception:
        pass
    return {"yt_dlp": ytdlp_version(), "curl_cffi": cc, "impersonation": impersonation_available()}


# ------------------------------------------------------------------ errors
class DownloadError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


# kind -> (regex, retry automatically?, counts toward the breaker?)
_KINDS = [
    ("cookies", r"cookie(s)? (database|file)|could not (find|copy) .*cookie|failed to decrypt|cookies from browser|"
                r"unsupported browser|keyring|could not find .* profile", False, True),
    ("blocked", r"unexpected response from webpage request|site under maintenance", True, True),
    ("rate_limited", r"429|too many requests|overload|rate.?limit", True, True),
    ("forbidden", r"http error 403|forbidden", True, True),
    ("login_required", r"login|log in|sign in|requiring login", False, True),
    ("unavailable", r"video (is )?unavailable|private|has been removed|no longer available|does not exist|"
                    r"http error 404|not available in your (country|region)|geo.?restrict", False, False),
    ("extract_failed", r"unable to extract|failed to parse json|unable to solve|challenge|no video formats", True, True),
    ("network", r"network (is )?down|offline|timed out|timeout|connection (reset|refused|aborted)|getaddrinfo|name or service|temporary failure "
                r"in name resolution|network is unreachable|ssl", True, False),
    ("no_file", r"produced no file", True, True),
]
KIND_RETRY = {k: r for k, _, r, _ in _KINDS} | {"other": True}
KIND_BREAKER = {k: b for k, _, _, b in _KINDS} | {"other": True}

HINTS = {
    "blocked": "TikTok is answering yt-dlp with a block page instead of the video (\"Unexpected response from "
               "webpage request\"). This is a TikTok-side bot check, not a Recall bug. Try, in order: update yt-dlp "
               "(restart Recall, run.sh/run.bat upgrade it), set 'Use cookies from my browser' to the browser you're "
               "logged into TikTok with, or paste your browser's User-Agent into Settings. Waiting an hour also helps "
               "if you downloaded a lot at once.",
    "forbidden": "TikTok refused the request (HTTP 403). Usually temporary rate-limiting of your IP or a fingerprint "
                 "check. Wait a while, update yt-dlp, or enable 'Use cookies from my browser'.",
    "rate_limited": "TikTok is rate-limiting this connection. Recall paused to stop making it worse; wait 30-60 minutes "
                    "and press Retry.",
    "cookies": "Recall couldn't read cookies from the selected browser. Close that browser completely and retry, or "
               "pick another browser (Firefox works most reliably; Chrome/Edge on Windows encrypt cookies in a way "
               "yt-dlp often can't read), or set it back to 'none'.",
    "login_required": "TikTok wants a logged-in session for these videos. Set 'Use cookies from my browser' to the "
                      "browser where you're logged into TikTok.",
    "extract_failed": "yt-dlp could not understand TikTok's page. TikTok changed something; update yt-dlp (restart "
                      "Recall) — if that doesn't help, the fix may only be in the yt-dlp nightly build.",
    "network": "The network looks down or TikTok is unreachable from here.",
    "unavailable": "The video is private, removed, or region-locked.",
    "other": "yt-dlp failed in an unexpected way. Update yt-dlp (restart Recall) and retry.",
    "no_file": "yt-dlp finished without producing a video file.",
}


def classify(msg: str) -> str:
    m = msg.lower()
    for kind, rx, _, _ in _KINDS:
        if re.search(rx, m):
            return kind
    return "other"


def concise(msg: str, limit: int = 160) -> str:
    """yt-dlp messages carry 'ERROR: [TikTok] <id>: ...; please report this issue on ...' — keep the useful part."""
    m = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    m = re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?(\d+:\s*)?", "", m.strip())
    m = re.split(r";\s*please report this issue|; Confirm you are on the latest", m, flags=re.I)[0]
    return m.strip()[:limit]


class _QuietLogger:
    """Swallows yt-dlp's own stdout/stderr printing; Recall logs one line per failed video instead."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


# ------------------------------------------------------------------ profiles
def profiles(settings: dict) -> list[tuple[str, dict]]:
    """Ordered request profiles for one download. Cookies (if chosen) apply to every profile."""
    ua = (settings.get("ytdlp_user_agent") or "").strip() or DESKTOP_UA
    mode = settings.get("ytdlp_impersonate", "auto")
    imp = impersonation_available() and mode != "never"
    out: list[tuple[str, dict]] = []
    plain = ("desktop-ua", {"http_headers": {"User-Agent": ua}})
    if imp:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        impersonate = ("impersonate", {"impersonate": ImpersonateTarget("chrome"), "http_headers": {"User-Agent": ua}})
        out += [impersonate, plain] if mode == "always" else [plain, impersonate]
    else:
        out.append(plain)
    out.append(("yt-dlp-default", {}))  # whatever the installed yt-dlp does by itself
    return out


def cookie_opts(settings: dict) -> dict:
    b = (settings.get("cookies_browser") or "none").lower()
    f = (settings.get("cookies_file") or "").strip()
    if f:
        return {"cookiefile": str(Path(f).expanduser())}
    if b in BROWSERS:
        return {"cookiesfrombrowser": (b, None, None, None)}
    return {}


_preferred: dict[str, str] = {}  # remembers the profile that last worked (per process)


def download(url: str, out_dir: Path, settings: dict, wait: Callable[[float], bool] = lambda s: (time.sleep(s), False)[1]
             ) -> tuple[Path, dict, str]:
    """Download one video. Tries each profile (sticky winner first) with a short backoff between attempts.
    Returns (path, metadata, profile_name). Raises DownloadError with a classified kind."""
    import yt_dlp
    out_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "format": "worst[ext=mp4][vcodec!=none][acodec!=none]/best[height<=720][ext=mp4]/worst/best",
        "quiet": True, "no_warnings": True, "noprogress": True, "no_color": True,
        "socket_timeout": 30, "retries": 1, "extractor_retries": 1, "noplaylist": True,
        **cookie_opts(settings),
    }
    profs = profiles(settings)
    if _preferred.get("name") in [n for n, _ in profs]:
        profs.sort(key=lambda p: p[0] != _preferred["name"])
    last: DownloadError | None = None
    for i, (name, extra) in enumerate(profs):
        if i and wait(2.0 * 2 ** (i - 1)):   # 2 s, 4 s between profiles; abort if asked to stop
            break
        lg = _QuietLogger()
        opts = {**base, **extra, "logger": lg}
        if "http_headers" in extra:
            opts["http_headers"] = {**extra["http_headers"]}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = Path(ydl.prepare_filename(info))
            if not path.exists():
                cands = list(out_dir.glob(f"{info.get('id')}.*"))
                if not cands:
                    raise DownloadError("no_file", "download produced no file")
                path = cands[0]
            _preferred["name"] = name
            meta = {k: info.get(k) for k in ("id", "title", "description", "duration", "track", "artist", "uploader",
                                             "uploader_id", "channel", "timestamp", "tags", "thumbnail")}
            return path, meta, name
        except DownloadError as e:
            last = e
        except Exception as e:  # yt_dlp.utils.DownloadError and friends
            raw = str(e) or (lg.errors[-1] if lg.errors else repr(e))
            kind = classify(raw)
            last = DownloadError(kind, concise(raw))
            log.debug("profile %s failed: %s", name, last)
            if kind in ("cookies", "unavailable", "login_required"):
                break  # changing the request profile won't help
    assert last is not None
    raise last


# ------------------------------------------------------------------ circuit breaker
class Breaker:
    """Counts consecutive download failures. After BREAKER_THRESHOLD in a row with the same error kind
    (or one cookie error) it opens: the deep queue pauses and the UI shows a banner with a Retry button.
    State is persisted in the settings table so the banner survives a restart."""

    KEY = "_download_breaker"
    _lock = threading.Lock()

    def __init__(self, con, threshold: int = BREAKER_THRESHOLD):
        self.con, self.threshold = con, threshold

    def state(self) -> dict:
        import json
        r = self.con.execute("SELECT value FROM settings WHERE key=?", (self.KEY,)).fetchone()
        base = {"open": False, "consecutive": 0, "kind": None, "message": None, "opened_at": None, "ids": []}
        if r:
            try:
                base.update(json.loads(r[0]))
            except Exception:
                pass
        return base

    def _save(self, st: dict) -> None:
        import json
        self.con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (self.KEY, json.dumps(st)))
        self.con.commit()

    def success(self) -> None:
        with self._lock:
            st = self.state()
            if st["consecutive"] or st["open"]:
                st.update(open=False, consecutive=0, kind=None, message=None, ids=[], opened_at=None)
                self._save(st)

    def failure(self, kind: str, message: str, video_id: str) -> dict:
        with self._lock:
            st = self.state()
            if not KIND_BREAKER.get(kind, True):
                return st
            if st["kind"] == kind:
                st["consecutive"] += 1
                st["ids"] = (st.get("ids") or [])[-9:] + [video_id]
            else:
                st.update(consecutive=1, kind=kind, ids=[video_id])
            st["message"] = message
            if not st["open"] and (st["consecutive"] >= self.threshold or kind == "cookies"):
                st.update(open=True, opened_at=time.time(), yt_dlp=ytdlp_version())
                log.warning("deep queue paused after %d consecutive download failures [%s]: %s",
                            st["consecutive"], kind, message)
            self._save(st)
            return st

    def reset(self) -> dict:
        with self._lock:
            st = self.state()
            st.update(open=False, consecutive=0, kind=None, message=None, ids=[], opened_at=None)
            self._save(st)
            return st

    def reset_if_upgraded(self) -> bool:
        """Called at startup: if yt-dlp changed since the breaker opened (run.sh upgrades it), try again."""
        st = self.state()
        if st["open"] and st.get("yt_dlp") != ytdlp_version():
            self.reset()
            log.info("yt-dlp is now %s (was %s) — download queue resumed", ytdlp_version(), st.get("yt_dlp"))
            return True
        return False

    def is_open(self) -> bool:
        return bool(self.state()["open"])

    def backoff_s(self) -> float:
        """Exponential pause before the next download while failures are piling up: 0, 5, 10, 20, 40, 60 s."""
        n = self.state()["consecutive"]
        return 0.0 if n == 0 else min(60.0, 5.0 * 2 ** (n - 1))

    def public(self) -> dict:
        st = self.state()
        st["hint"] = HINTS.get(st["kind"] or "", HINTS["other"]) if st["kind"] else None
        st["threshold"] = self.threshold
        return st
