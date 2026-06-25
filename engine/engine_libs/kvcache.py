"""Tiny persistent key→JSON cache, one file per namespace.

Used to memoize slow lookups that aren't audio features:
  - WhoSampled scrapes   (artist  → list of sample sources)
  - YouTube track search (artist+title → resolved video)

Entries optionally expire after `ttl_days`. Thread-safe enough for this
single-process app via a coarse lock per namespace.
"""
import json
import threading
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "kv"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_locks: dict[str, threading.Lock] = {}
_mem: dict[str, dict] = {}   # namespace → loaded dict


def _lock(ns: str) -> threading.Lock:
    return _locks.setdefault(ns, threading.Lock())


def _path(ns: str) -> Path:
    return CACHE_DIR / f"{ns}.json"


def _load(ns: str) -> dict:
    if ns in _mem:
        return _mem[ns]
    p = _path(ns)
    if p.exists():
        try:
            _mem[ns] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _mem[ns] = {}
    else:
        _mem[ns] = {}
    return _mem[ns]


def get(ns: str, key: str, ttl_days: float | None = None):
    """Return cached value for key, or None if missing/expired."""
    with _lock(ns):
        rec = _load(ns).get(key.strip().lower())
    if not rec:
        return None
    if ttl_days is not None:
        age_days = (time.time() - rec.get("ts", 0)) / 86400
        if age_days > ttl_days:
            return None
    return rec.get("val")


def put(ns: str, key: str, val):
    with _lock(ns):
        d = _load(ns)
        d[key.strip().lower()] = {"ts": time.time(), "val": val}
        _path(ns).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def count(ns: str) -> int:
    with _lock(ns):
        return len(_load(ns))
