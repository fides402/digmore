"""YouTube layer built on yt-dlp (no API key needed).

Strategy:
  1. Search YouTube for *playlists* of "<genre> samples"
     — same filter as ?sp=EgIQAw%3D%3D (type=playlist) in the browser.
  2. Pick the top playlists found, pull a random slice of their track list.
  3. download_snippet(video_id): downloads seconds 30-90 as wav for analysis.
"""
import random
import re
import threading
import urllib.parse
from pathlib import Path

import llm_filter

# Fast keyword pre-filter for individual video titles in OST mode.
# Catches obvious gaming content without an LLM call per video.
# Deliberately conservative: requires game-specific terms, not just "game".
_GAME_RE = re.compile(
    r"\b("
    r"video\s*game|game\s*ost|game\s*music|game\s*soundtrack|gaming\s*(music|ost|soundtrack)|"
    r"nintendo|playstation|xbox\s*(one|360|series)?|"
    r"zelda|mario\s*(kart|galaxy|odyssey|64)?|pok[eé]mon|minecraft|sonic|"
    r"halo|final\s*fantasy|skyrim|elder\s*scrolls|dark\s*souls|elden\s*ring|"
    r"fortnite|roblox|valorant|overwatch|league\s*of\s*legends|"
    r"genshin(\s*impact)?|persona\s*\d|kingdom\s*hearts|"
    r"chrono\s*trigger|undertale|hollow\s*knight|celeste|"
    r"metal\s*gear|resident\s*evil|silent\s*hill|"
    r"rpg\s*(game\s*)?music|jrpg|game\s*(boss|battle|theme|bgm|bgmusic)"
    r")\b",
    re.IGNORECASE,
)

import os as _os
from yt_dlp import YoutubeDL as _RealYoutubeDL
from yt_dlp.utils import download_range_func

import kvcache

SNIPPET_DIR = Path(__file__).resolve().parent / ".cache" / "snippets"
SNIPPET_DIR.mkdir(parents=True, exist_ok=True)


# ── YouTube cookies (bypass datacenter-IP bot detection on Hugging Face) ──────
# YouTube blocks requests from cloud/datacenter IPs ("Sign in to confirm you're
# not a bot"). Providing the user's browser cookies makes yt-dlp authenticate as
# a logged-in user, which lifts the block. Two ways to supply them:
#   * YT_COOKIES_FILE = path to a Netscape cookies.txt
#   * YT_COOKIES      = the cookies.txt *content* (e.g. an HF Secret) — written
#                       to a temp file once at import.
def _resolve_cookiefile():
    p = _os.environ.get("YT_COOKIES_FILE", "").strip()
    if p and Path(p).is_file():
        return p
    raw = _os.environ.get("YT_COOKIES", "")
    if raw.strip():
        dst = SNIPPET_DIR.parent / "yt_cookies.txt"
        try:
            dst.write_text(raw, encoding="utf-8")
            return str(dst)
        except Exception:
            return None
    return None


_COOKIEFILE = _resolve_cookiefile()


_COOKIES_HARDPATH = Path(r"C:\Users\User\Downloads\sre\digmore\yt_cookies.txt")

def _get_cookiefile():
    """Return cookie file path: env var → hardcoded fallback."""
    if _COOKIEFILE:
        return _COOKIEFILE
    rt = _resolve_cookiefile()
    if rt:
        return rt
    if _COOKIES_HARDPATH.is_file():
        print(f"[yt_hunter] cookie: {_COOKIES_HARDPATH}", flush=True)
        return str(_COOKIES_HARDPATH)
    print(f"[yt_hunter] NESSUN cookie trovato!", flush=True)
    return None


# Player-client override. Locally (residential IP) yt-dlp's DEFAULT client
# selection returns downloadable non-DRM audio formats — forcing web/tv/ios
# instead yields DRM-only streams that fail with "This video is DRM protected".
# So we only override when YT_PLAYER_CLIENTS is *explicitly* set (e.g. on HF,
# where the default client's innertube is blocked from the datacenter IP).
_PLAYER_CLIENTS = [
    c.strip() for c in _os.environ.get("YT_PLAYER_CLIENTS", "").split(",")
    if c.strip()
]


_FFMPEG_PATH = r"C:\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"

def YoutubeDL(opts=None, *args, **kwargs):
    """yt-dlp wrapper: inject cookies + Node.js EJS solver + ffmpeg path."""
    opts = dict(opts or {})
    cf = _get_cookiefile()
    if cf and "cookiefile" not in opts and "cookiesfrombrowser" not in opts:
        opts["cookiefile"] = cf
    # Node.js + EJS solver da GitHub: risolve le challenge JS di YouTube senza bot-check
    if "js_runtimes" not in opts:
        opts["js_runtimes"] = {"node": {}}
    if "remote_components" not in opts:
        opts["remote_components"] = ["ejs:github"]
    if _PLAYER_CLIENTS:
        ea = dict(opts.get("extractor_args") or {})
        if "youtube" not in ea:
            ea["youtube"] = {"player_client": _PLAYER_CLIENTS}
            opts["extractor_args"] = ea
    import os
    if os.path.exists(_FFMPEG_PATH) and "ffmpeg_location" not in opts:
        opts["ffmpeg_location"] = _FFMPEG_PATH
    return _RealYoutubeDL(opts, *args, **kwargs)

_YT_SEARCH_NS = "yt_search"   # kvcache namespace: "artist::title" → resolved video

# YouTube search filter that shows only Playlists
_SP_PLAYLISTS = "EgIQAw%3D%3D"

# Query templates — shuffled each call so different playlists surface each round
_QUERIES = [
    "{g} samples",
    "{g} sample pack",
    "{g} samples to flip",
    "{g} vinyl samples",
    "{g} rare samples",
    "{g} crates",
    "{g} breakbeats",
    "{g} loops",
    "{g} instrumental",
    "{g} music samples",
    "vintage {g} samples",
    "{g} sample flip",
]


def _search_playlists(genre: str, max_playlists: int = 10) -> list[str]:
    """Return YouTube playlist IDs from a playlist-filtered search.

    Shuffles the query templates each call so different playlists surface on
    repeat invocations with the same genre.

    OST mode: each playlist title is checked via LLM before being added —
    video game OST playlists are silently skipped.
    """
    is_ost = genre.lower() == "ost"
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True}
    seen, ids = set(), []
    queries = list(_QUERIES)
    random.shuffle(queries)
    with YoutubeDL(opts) as ydl:
        for tpl in queries:
            if len(ids) >= max_playlists:
                break
            q = urllib.parse.quote_plus(tpl.format(g=genre))
            url = f"https://www.youtube.com/results?search_query={q}&sp={_SP_PLAYLISTS}"
            try:
                info = ydl.extract_info(url, download=False)
            except Exception:
                continue
            for e in (info.get("entries") or []):
                pid = e.get("id") or e.get("playlist_id") or ""
                if not pid or pid in seen:
                    continue
                if is_ost:
                    title = (e.get("title") or e.get("playlist_title") or "")
                    if title and llm_filter.is_game_ost(title):
                        print(f"[yt_hunter] OST filter: skipped game playlist '{title[:60]}'")
                        continue
                seen.add(pid)
                ids.append(pid)
                if len(ids) >= max_playlists:
                    break
    random.shuffle(ids)
    return ids


def _playlist_tracks(playlist_id: str, limit: int = 80) -> list[dict]:
    """Return flat video entries from a playlist (no download)."""
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True,
            "playlistend": limit}
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception:
            return []
    tracks = []
    for e in (info.get("entries") or []):
        vid = e.get("id")
        if not vid:
            continue
        dur = e.get("duration") or 0
        # skip shorts (<60s) and compilations (>15min)
        if not (60 <= dur <= 900):
            continue
        # skip sample pack / tutorial videos
        title_low = (e.get("title") or "").lower()
        if "sample pack" in title_low:
            continue
        tracks.append({
            "video_id": vid,
            "title": e.get("title", ""),
            "channel": e.get("uploader") or e.get("channel", ""),
            "duration": dur,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return tracks


def search_candidates(genre: str, n: int = 30,
                      exclude: set = None) -> list[dict]:
    """Return up to `n` shuffled candidate videos drawn from genre playlists.

    exclude: set of video_id strings already analyzed — filtered out so each
    call returns fresh candidates even with the same genre.
    """
    playlist_ids = _search_playlists(genre, max_playlists=10)

    seen, pool = set(), []
    for pid in playlist_ids:
        tracks = _playlist_tracks(pid, limit=100)
        for t in tracks:
            if t["video_id"] not in seen:
                seen.add(t["video_id"])
                pool.append(t)

    if not pool:
        pool = _fallback_video_search(genre, n)

    if exclude:
        pool = [t for t in pool if t["video_id"] not in exclude]

    # OST mode: fast keyword filter on individual video titles (no LLM per video).
    # This catches obvious game tracks that slipped through non-game playlists.
    if genre.lower() == "ost":
        before = len(pool)
        pool = [t for t in pool if not _GAME_RE.search(t.get("title", ""))]
        skipped = before - len(pool)
        if skipped:
            print(f"[yt_hunter] OST keyword filter: skipped {skipped} game tracks")

    random.shuffle(pool)
    return pool[:n]


def _fallback_video_search(genre: str, n: int) -> list[dict]:
    """Fallback: search individual videos (less reliable than playlists)."""
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True}
    seen, pool = set(), []
    with YoutubeDL(opts) as ydl:
        for q in [f"{genre} sample music", f"{genre} instrumental"]:
            try:
                info = ydl.extract_info(f"ytsearch15:{q}", download=False)
            except Exception:
                continue
            for e in (info.get("entries") or []):
                vid = e.get("id")
                dur = e.get("duration") or 0
                title_low = (e.get("title") or "").lower()
                if vid and vid not in seen and 60 <= dur <= 900 and "sample pack" not in title_low:
                    seen.add(vid)
                    pool.append({
                        "video_id": vid,
                        "title": e.get("title", ""),
                        "channel": e.get("uploader") or e.get("channel", ""),
                        "duration": dur,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                    })
    return pool[:n]


# Title words that signal a re-recording / wrong version — we want the ORIGINAL
# studio record the producer actually sampled, not a cover or a live take.
_BAD_WORDS = ("live", "cover", "remix", "karaoke", "instrumental", "reaction",
              "tutorial", "lesson", "sped up", "slowed", "8d", "nightcore",
              "reverb", "loop", "type beat", "mashup", "edit", "remaster")


def _tokset(s: str) -> set[str]:
    return {t for t in re.split(r"\W+", s.lower()) if len(t) > 1}


def _rank_entry(e: dict, artist: str, title: str) -> float:
    """Higher = more likely the original studio version of artist–title."""
    name = (e.get("title") or "").lower()
    score = 0.0
    # reward title/artist token overlap
    want = _tokset(artist) | _tokset(title)
    have = _tokset(name)
    if want:
        score += 3.0 * len(want & have) / len(want)
    # penalize non-original versions
    score -= sum(1.5 for w in _BAD_WORDS if w in name)
    # prefer "official"/"audio" uploads, mild bonus
    if "official" in name or "audio" in name:
        score += 0.5
    # duration sanity: most sampled records sit 1.5–8 min
    dur = e.get("duration") or 0
    if 90 <= dur <= 480:
        score += 0.5
    return score


def search_yt_for_track(artist: str, title: str, use_cache: bool = True) -> dict | None:
    """Resolve the best YouTube video for a specific track.

    Instead of grabbing the first hit, we pull several results and rank them so
    covers / live / remix / sped-up versions don't get analyzed in place of the
    original record. The chosen video (or a 'not found' marker) is cached.
    """
    ck = f"{artist}::{title}"
    if use_cache:
        hit = kvcache.get(_YT_SEARCH_NS, ck)
        if hit is not None:
            return hit or None   # {} marker means "searched, none found"

    queries = [
        f"{artist} - {title}",
        f"{artist} {title}",
    ]
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True,
            "socket_timeout": 8}
    best, best_score = None, -1e9
    with YoutubeDL(opts) as ydl:
        for query in queries:
            try:
                info = ydl.extract_info(f"ytsearch8:{query}", download=False)
            except Exception:
                continue
            for e in (info.get("entries") or []):
                dur = e.get("duration") or 0
                vid = e.get("id")
                if not vid or not (30 <= dur <= 1200):
                    continue
                s = _rank_entry(e, artist, title)
                if s > best_score:
                    best_score, best = s, {
                        "video_id": vid,
                        "title":    e.get("title", query),
                        "channel":  e.get("uploader") or e.get("channel", ""),
                        "duration": dur,
                        "url":      f"https://www.youtube.com/watch?v={vid}",
                    }
            # a confidently-good hit on an early query is enough
            if best is not None and best_score >= 3.0:
                break
    if best is not None:
        if use_cache:
            kvcache.put(_YT_SEARCH_NS, ck, best)
        return best
    if use_cache:
        kvcache.put(_YT_SEARCH_NS, ck, {})   # remember the miss (don't re-search)
    return None


_EMBED_NS = "yt_embeddable"   # kvcache: video_id → bool


_YT_API_KEY = _os.environ.get("YT_API_KEY", "").strip()
# Region the player runs in — used to honour YouTube region restrictions.
_PLAYER_REGION = _os.environ.get("YT_REGION", "IT").strip().upper()


def _embeddable_via_data_api(video_id: str) -> bool | None:
    """Authoritative embeddability check via the YouTube Data API.

    status.embeddable is the exact flag the IFrame player honours. Also rejects
    unprocessed/private videos and ones region-blocked where the player runs.
    Returns None if the API can't answer (so caller can fall back).
    """
    import json
    import urllib.request
    import urllib.error
    url = ("https://www.googleapis.com/youtube/v3/videos"
           f"?part=status,contentDetails&id={video_id}&key={_YT_API_KEY}")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None          # quota/key problem → let caller fall back
    except Exception:
        return None

    items = data.get("items") or []
    if not items:
        return False         # removed / private / nonexistent
    it = items[0]
    st = it.get("status", {}) or {}
    if not st.get("embeddable", False):
        return False         # owner disabled embedding → player error 150
    if st.get("uploadStatus") != "processed":
        return False         # still processing / rejected
    if st.get("privacyStatus") not in ("public", "unlisted"):
        return False

    rr = (it.get("contentDetails", {}) or {}).get("regionRestriction", {}) or {}
    blocked = rr.get("blocked")
    allowed = rr.get("allowed")
    if blocked and _PLAYER_REGION in blocked:
        return False
    if allowed is not None and _PLAYER_REGION not in allowed:
        return False
    return True


def _embeddable_via_embed_page(video_id: str) -> bool | None:
    """Ground-truth check: fetch the actual embed page and parse playabilityStatus.

    This is exactly what the IFrame player does internally. Catches Content ID
    restrictions that status.embeddable and oEmbed both miss.
    Returns None on network errors so the caller can fall back.
    """
    import json
    import re
    import urllib.request
    url = f"https://www.youtube.com/embed/{video_id}?hl=en"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None   # network error → caller falls back

    idx = html.find('ytInitialPlayerResponse')
    if idx == -1:
        return None
    idx = html.find('{', idx)
    if idx == -1:
        return None
    depth = 0
    data = None
    for i, c in enumerate(html[idx:], idx):
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(html[idx:i + 1])
                except Exception:
                    return None
                break
    if data is None:
        return None

    ps = data.get("playabilityStatus") or {}
    status = ps.get("status", "")
    # Solo OK è garantito nell'IFrame senza login
    if status == "OK":
        return True
    # CONTENT_CHECK_REQUIRED, AGE_VERIFICATION_REQUIRED, LOGIN_REQUIRED → non funziona nell'IFrame
    return False


def _embeddable_via_oembed(video_id: str) -> bool:
    """Last-resort check via YouTube's oEmbed endpoint (no API key).

    200 = embeddable+available; 401/403 = embedding disabled; 404 = unavailable.
    Note: oEmbed misses Content ID restrictions, use embed-page check first.
    """
    import urllib.request
    import urllib.error
    url = ("https://www.youtube.com/oembed?format=json&url="
           + urllib.parse.quote(
               f"https://www.youtube.com/watch?v={video_id}", safe=""))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False  # errore di rete → scarta, meglio un falso negativo che un falso positivo


def is_embeddable(video_id: str, use_cache: bool = True) -> bool:
    """True if the video can actually play in an off-site IFrame embed.

    Chain: embed-page parse (ground truth) → Data API (fast, misses Content ID)
    → oEmbed (last resort). Cached per video_id.
    """
    if use_cache:
        hit = kvcache.get(_EMBED_NS, video_id)
        if hit is not None:
            return bool(hit)

    # Primary: parse the actual embed page — same check the IFrame player does.
    ok = _embeddable_via_embed_page(video_id)
    # Fallback 1: Data API (fast but misses Content ID restrictions)
    if ok is None and _YT_API_KEY:
        ok = _embeddable_via_data_api(video_id)
    # Fallback 2: oEmbed (least reliable but requires no key)
    if ok is None:
        ok = _embeddable_via_oembed(video_id)

    if use_cache:
        kvcache.put(_EMBED_NS, video_id, ok)
    return ok


def download_snippet(video_id: str, start: int = 40, dur: int = 30) -> str:
    """Download a short snippet of a video as mp3; return the file path.

    DIGMORE scores tracks purely on a single CLAP embedding, so a short ~30s
    window from the body of the track is enough — far faster to download and
    transcode than the full 150s span. MP3 at 128kbps is plenty for CLAP.
    """
    out_base = SNIPPET_DIR / video_id
    target = out_base.with_suffix(".mp3")

    # Rimuovi file parziali/corrotti lasciati da interruzioni (es. riavvio PC)
    for stale in SNIPPET_DIR.glob(f"{video_id}.*"):
        if stale.suffix in (".part", ".ytdl") or (stale != target and stale.stat().st_size == 0):
            try:
                stale.unlink()
            except OSError:
                pass

    if target.exists() and target.stat().st_size > 0:
        return str(target)

    opts = {
        "quiet": True, "no_warnings": True,
        # Prefer the classic progressive/DASH audio itags (140 m4a, 251 opus,
        # 139 m4a-low). "bestaudio" alone can resolve to a DRM/SABR stream that
        # fails with "This video is DRM protected" even on non-DRM videos.
        "format": "140/251/139/bestaudio[has_drm=0]/bestaudio/best",
        "outtmpl": str(out_base) + ".%(ext)s",
        "download_ranges": download_range_func(None, [(start, start + dur)]),
        "force_keyframes_at_cuts": True,
        "socket_timeout": 30,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    }
    exc_holder: list[Exception] = []

    def _do_download():
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_do_download, daemon=True)
    t.start()
    t.join(timeout=120)
    if t.is_alive():
        raise TimeoutError(f"download timeout for {video_id}")
    if exc_holder:
        raise exc_holder[0]

    if not target.exists():
        hits = list(SNIPPET_DIR.glob(f"{video_id}.*"))
        if not hits:
            raise FileNotFoundError(f"snippet not produced for {video_id}")
        target = hits[0]
    return str(target)


def get_yt_mix(video_id: str, limit: int = 40) -> list[dict]:
    """Fetch the YouTube Radio/Mix playlist for a video (list=RD{video_id}).

    YouTube's recommender builds this mix from billions of listening patterns —
    it surfaces audio-similar tracks far better than any text-based search.
    """
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = {"quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True,
            "playlistend": limit}
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception:
            return []
    tracks = []
    for e in (info.get("entries") or []):
        vid = e.get("id")
        if not vid or vid == video_id:
            continue
        dur = e.get("duration") or 0
        if not (30 <= dur <= 900):
            continue
        title_low = (e.get("title") or "").lower()
        if "sample pack" in title_low:
            continue
        tracks.append({
            "video_id": vid,
            "title":    e.get("title", ""),
            "channel":  e.get("uploader") or e.get("channel", ""),
            "duration": dur,
            "url":      f"https://www.youtube.com/watch?v={vid}",
        })
    return tracks


def cleanup_snippets() -> int:
    """Delete all cached audio snippets (mp3/wav) from this session's downloads.

    Call once at startup. The heavy audio files are only needed during analysis;
    the extracted feature vectors (NPZ in .cache/yt/) are kept permanently.
    Returns the number of files deleted.
    """
    deleted = 0
    for f in SNIPPET_DIR.iterdir():
        if f.is_file():
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    if deleted:
        print(f"[cache] Cleaned {deleted} snippet(s) from previous session.")
    return deleted
