"""Persistent 'already listened' store.

Tracks the user has heard -- both imported from outside the app (CSV / pasted
``artist,title`` lines) and auto-marked when a track is played in DIGMORE.
Anything in here is excluded from every future playlist generation.

Persisted as JSON under DATA_DIR so it survives restarts (and the HF persistent
volume). Two indexes:
  * track keys  -- normalized "artist::title"
  * video ids   -- resolved YouTube ids that were played
"""
import csv
import io
import json
import re
import threading

from paths_boot import DATA_DIR

_PATH = DATA_DIR / "listened.json"
_LOCK = threading.Lock()

_DISAMBIG = re.compile(r"\s*\(\d+\)\s*$")     # Discogs "Artist (2)"
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*$", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = _DISAMBIG.sub("", s)
    s = _FEAT.sub("", s)
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


def track_key(artist: str, title: str) -> str:
    return f"{_norm(artist)}::{_norm(title)}"


def _load() -> dict:
    if _PATH.exists():
        try:
            d = json.loads(_PATH.read_text(encoding="utf-8"))
            d.setdefault("tracks", [])
            d.setdefault("videos", [])
            return d
        except Exception:
            pass
    return {"tracks": [], "videos": []}


def _save(d: dict):
    _PATH.write_text(json.dumps(d, ensure_ascii=False, indent=0), encoding="utf-8")


def contains(artist: str, title: str) -> bool:
    with _LOCK:
        return track_key(artist, title) in set(_load()["tracks"])


def contains_video(video_id: str) -> bool:
    with _LOCK:
        return video_id in set(_load()["videos"])


def add(artist: str, title: str, video_id: str = "") -> None:
    with _LOCK:
        d = _load()
        tk = track_key(artist, title)
        tracks = set(d["tracks"]); videos = set(d["videos"])
        if tk.strip("::"):
            tracks.add(tk)
        if video_id:
            videos.add(video_id)
        d["tracks"] = sorted(tracks); d["videos"] = sorted(videos)
        _save(d)


def import_csv(text: str) -> dict:
    """Parse pasted/uploaded text into track keys and merge them in.

    Accepts CSV with a header (columns matched by name: artist/title) or
    plain "artist, title" / "artist - title" lines. Title-only lines are
    stored with an empty artist.
    Returns {"added": n, "total": m}.
    """
    keys = _parse_rows(text)
    with _LOCK:
        d = _load()
        before = len(d["tracks"])
        tracks = set(d["tracks"]) | keys
        d["tracks"] = sorted(tracks)
        _save(d)
        return {"added": len(d["tracks"]) - before, "total": len(d["tracks"])}


def _parse_rows(text: str) -> set[str]:
    text = (text or "").strip()
    if not text:
        return set()
    keys: set[str] = set()

    # Try structured CSV with header first.
    sample = text.splitlines()[0].lower()
    has_header = any(h in sample for h in ("artist", "title", "track", "song", "name"))
    if has_header and ("," in text or "\t" in text or ";" in text):
        dialect_delims = [",", ";", "\t"]
        delim = max(dialect_delims, key=lambda c: sample.count(c))
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        a_col = _pick(reader.fieldnames, (
                "artist name(s)", "artist names", "artist", "artists", "albumartist", "author"))
        t_col = _pick(reader.fieldnames, (
                "track name", "track", "title", "song", "name", "trackname"))
        if t_col:
            for row in reader:
                a = (row.get(a_col) or "") if a_col else ""
                t = row.get(t_col) or ""
                if t.strip():
                    keys.add(track_key(a, t))
            return keys

    # Fallback: line-by-line "artist - title" / "artist, title" / "title".
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        a, t = "", line
        for sep in (" - ", " – ", "\t", ";", ","):
            if sep in line:
                a, t = line.split(sep, 1)
                break
        keys.add(track_key(a.strip(), t.strip()))
    return keys


def _pick(fields, names):
    if not fields:
        return None
    low = {f.lower().strip(): f for f in fields}
    for n in names:
        if n in low:
            return low[n]
    return None


def stats() -> dict:
    with _LOCK:
        d = _load()
        return {"tracks": len(d["tracks"]), "videos": len(d["videos"])}
