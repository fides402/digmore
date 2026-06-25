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


def YoutubeDL(opts=None, *args, **kwargs):
    """yt-dlp wrapper that injects the cookie file (if configured) everywhere."""
    opts = dict(opts or {})
    if _COOKIEFILE and "cookiefile" not in opts and "cookiesfrombrowser" not in opts:
        opts["cookiefile"] = _COOKIEFILE
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


def download_snippet(video_id: str, start: int = 40, dur: int = 30) -> str:
    """Download a short snippet of a video as mp3; return the file path.

    DIGMORE scores tracks purely on a single CLAP embedding, so a short ~30s
    window from the body of the track is enough — far faster to download and
    transcode than the full 150s span. MP3 at 128kbps is plenty for CLAP.
    """
    out_base = SNIPPET_DIR / video_id
    target = out_base.with_suffix(".mp3")
    if target.exists():
        return str(target)

    opts = {
        "quiet": True, "no_warnings": True,
        "format": "bestaudio/best",
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
