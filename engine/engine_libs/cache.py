"""Per-video feature cache (so re-runs don't re-download / re-analyze).

Stores the FULL feature set extracted by features.extract():
  - 7 vector features (chroma/mfcc/contrast/tonnetz/spec/tempogram/clap) → one .npz
  - scalar features (key/mode/camelot/bpm/…) + meta → JSON index

A cache version guards against stale entries written by older code that only
saved a subset of vectors — those are treated as a miss and re-analyzed.
"""
import json
import threading
from pathlib import Path

import numpy as np

# Candidate analysis runs on several worker threads, so the read-modify-write
# of index.json must be serialized or concurrent puts would clobber each other.
_LOCK = threading.Lock()

CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "yt"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
INDEX = CACHE_DIR / "index.json"

# Bump when the feature schema changes → old entries auto-invalidate.
# v3: candidate vectors are now 2-D (n_segments × dim) from multi-segment +
#     HPSS drum-robust extraction; old 1-D entries must be re-analyzed.
CACHE_VERSION = 3

# All vector features the scorer needs (numpy arrays). 'clap' may be None.
VECTOR_KEYS = [
    "chroma_mean", "mfcc_mean", "contrast_mean",
    "tonnetz_mean", "spec_vec", "tempogram_mean", "clap",
]
# Scalar / small features stored in the JSON index.
SCALAR_KEYS = [
    "key", "mode", "camelot", "key_conf", "bpm",
    "onset_density", "rms", "zcr", "centroid", "rolloff", "band_energy",
]


def _load_index() -> dict:
    if INDEX.exists():
        try:
            return json.loads(INDEX.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _jsonable(v):
    """Coerce numpy scalars / arrays to JSON-serializable Python types."""
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


def get(video_id: str) -> dict | None:
    idx = _load_index()
    meta = idx.get(video_id)
    if not meta or meta.get("_v") != CACHE_VERSION:
        return None
    npz = CACHE_DIR / f"{video_id}.npz"
    if not npz.exists():
        return None
    try:
        data = np.load(npz)
    except Exception:
        return None
    # Require every vector present, else treat as stale (re-extract).
    if not all(k in data.files for k in VECTOR_KEYS):
        return None

    feat = {k: v for k, v in meta.items() if not k.startswith("_")}
    for k in VECTOR_KEYS:
        arr = data[k]
        # CLAP (and any optional vector) is stored as length-0 when it was None.
        feat[k] = arr if arr.size > 0 else None
    return feat


def put(video_id: str, feat: dict, meta: dict):
    arrays = {}
    for k in VECTOR_KEYS:
        v = feat.get(k)
        arrays[k] = (v.astype(np.float32) if isinstance(v, np.ndarray)
                     else np.zeros(0, "float32"))
    np.savez(CACHE_DIR / f"{video_id}.npz", **arrays)

    with _LOCK:
        idx = _load_index()
        idx[video_id] = {
            **meta,
            **{k: _jsonable(feat.get(k)) for k in SCALAR_KEYS},
            "_v": CACHE_VERSION,
        }
        INDEX.write_text(json.dumps(idx, indent=2), encoding="utf-8")


def stats() -> dict:
    """Quick counts for diagnostics."""
    idx = _load_index()
    return {"cached_videos": sum(1 for m in idx.values()
                                 if m.get("_v") == CACHE_VERSION)}
