"""Musical profiles: CSV reference lists -> composite CLAP embedding.

Each profile (chill soul, relax jazz, relax ost) is a curated CSV of
reference tracks. We resolve each track on YouTube, pull a 30s snippet, extract
its CLAP embedding, average + normalize them into one (512,) vector that
represents the profile's "vibe". That vector is the reference the digger scores
Discogs candidates against.

The composite embedding is cached on disk (DATA_DIR/profiles/<id>.npy) so it is
built once and reused across sessions / generations.

CSV format is auto-detected:
  * with a header -> columns matched by name (artist/title/track/song/name)
  * 2 columns no header -> assumed (artist, title)
  * 1 column        -> title only (resolved by title)
"""
import csv
import io
import json
import threading
from pathlib import Path

import numpy as np

import paths_boot  # noqa: F401  -- engine_libs on sys.path
from paths_boot import DATA_DIR, CSV_DIR

_PROFILE_DIR = DATA_DIR / "profiles"
_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# Registry: profile id -> display label + CSV filename + Discogs macrogenre key.
PROFILES: dict[str, dict] = {
    "soul": {"label": "Chill Soul", "csv": "chill_soul.csv"},
    "jazz": {"label": "Relax Jazz", "csv": "relax_jazz.csv"},
    "ost":  {"label": "Relax OST",  "csv": "relax_ost.csv"},
}

_build_locks: dict[str, threading.Lock] = {}


def _lock(pid: str) -> threading.Lock:
    return _build_locks.setdefault(pid, threading.Lock())


def _emb_path(pid: str) -> Path:
    return _PROFILE_DIR / f"{pid}.npy"


def _meta_path(pid: str) -> Path:
    return _PROFILE_DIR / f"{pid}.json"


def csv_path(pid: str) -> Path:
    return CSV_DIR / PROFILES[pid]["csv"]


# ── CSV parsing ──────────────────────────────────────────────────────────────

def read_reference_tracks(pid: str) -> list[tuple[str, str]]:
    """Return [(artist, title)] from the profile CSV, auto-detecting columns."""
    p = csv_path(pid)
    if not p.exists():
        raise FileNotFoundError(f"CSV mancante per profilo '{pid}': {p}")
    text = p.read_text(encoding="utf-8-sig")
    return _parse(text)


def _parse(text: str) -> list[tuple[str, str]]:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    header = lines[0].lower()
    delim = max([",", ";", "\t"], key=lambda c: header.count(c))
    has_header = any(h in header for h in ("artist", "title", "track", "song", "name"))

    out: list[tuple[str, str]] = []
    if has_header:
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        # Spotify exports use "Track Name" and "Artist Name(s)"; also match
        # bare "artist"/"title" for generic CSV exports.
        a_col = _pick(reader.fieldnames, (
            "artist name(s)", "artist names", "artist", "artists", "albumartist", "author"))
        t_col = _pick(reader.fieldnames, (
            "track name", "track", "title", "song", "name", "trackname"))
        for row in reader:
            a_raw = (row.get(a_col) or "").strip() if a_col else ""
            # Spotify lists multiple artists with ";" — keep only the first
            a = a_raw.split(";")[0].strip()
            t = (row.get(t_col) or "").strip() if t_col else ""
            if t:
                out.append((a, t))
        return out

    # No header: split on the detected delimiter.
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    for row in reader:
        cells = [c.strip() for c in row if c.strip()]
        if not cells:
            continue
        if len(cells) == 1:
            out.append(("", cells[0]))
        else:
            out.append((cells[0], cells[1]))
    return out


def _pick(fields, names):
    if not fields:
        return None
    low = {f.lower().strip(): f for f in fields}
    for n in names:
        if n in low:
            return low[n]
    return None


# ── Embedding build / load ───────────────────────────────────────────────────

def get_embedding(pid: str) -> np.ndarray | None:
    p = _emb_path(pid)
    return np.load(str(p)) if p.exists() else None


def load_meta(pid: str) -> dict:
    p = _meta_path(pid)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def status(pid: str) -> dict:
    info = {"id": pid, "label": PROFILES[pid]["label"],
            "csv_present": csv_path(pid).exists()}
    if _emb_path(pid).exists():
        meta = load_meta(pid)
        info["state"] = "ready"
        info["n_tracks"] = meta.get("n_tracks", 0)
    elif info["csv_present"]:
        info["state"] = "not_built"
    else:
        info["state"] = "no_csv"
    return info


def list_status() -> list[dict]:
    return [status(pid) for pid in PROFILES]


def build_embedding(pid: str, progress_cb=None) -> np.ndarray:
    """Build + cache the composite CLAP embedding for a profile.

    Mirrors SampleHunter's listening_profile.build_profile but parameterized
    per profile and sourced from the CSV reference list.
    """
    import yt_hunter
    import features

    refs = read_reference_tracks(pid)
    if not refs:
        raise RuntimeError(f"CSV vuoto per profilo '{pid}'")

    with _lock(pid):
        n = len(refs)
        embeddings: list[np.ndarray] = []
        resolved: list[dict] = []

        for i, (artist, title) in enumerate(refs):
            label = f"{artist} – {title}".strip(" –")
            if progress_cb:
                progress_cb(i, n, f"YouTube: {label}…")
            try:
                yt = yt_hunter.search_yt_for_track(artist, title)
                if not yt:
                    if progress_cb:
                        progress_cb(i, n, f"Non trovato: {label} — salto")
                    continue
                snippet = yt_hunter.download_snippet(yt["video_id"])
                if progress_cb:
                    progress_cb(i, n, f"CLAP: {yt['title']}…")
                feat = features.extract(snippet, with_clap=True,
                                        drum_robust=True, segment=False)
                clap = feat.get("clap")
                if clap is None:
                    continue
                emb = np.asarray(clap, dtype=np.float32)
                if emb.ndim == 2:
                    emb = emb.mean(axis=0)
                emb = emb / (np.linalg.norm(emb) + 1e-9)
                embeddings.append(emb)
                resolved.append({"artist": artist, "title": title,
                                 "yt_title": yt["title"], "video_id": yt["video_id"]})
            except Exception as e:
                if progress_cb:
                    progress_cb(i, n, f"Errore {label}: {e}")

        if not embeddings:
            raise RuntimeError(f"Nessun brano reference trovato per '{pid}'.")

        mean = np.stack(embeddings).mean(axis=0)
        profile = (mean / (np.linalg.norm(mean) + 1e-9)).astype(np.float32)

        np.save(str(_emb_path(pid)), profile)
        _meta_path(pid).write_text(
            json.dumps({"id": pid, "n_tracks": len(resolved), "tracks": resolved},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        if progress_cb:
            progress_cb(n, n, f"Profilo '{pid}' pronto — {len(resolved)}/{n} tracce")
        return profile
