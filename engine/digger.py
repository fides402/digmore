"""Playlist orchestrator -- the heart of DIGMORE.

Given a profile embedding, repeatedly pull random lesser-known, well-rated
Discogs releases (1969-1983), resolve each track on YouTube, extract its CLAP
embedding and score it against the profile. Keep only tracks above the vibe
gate (default 55%) that the user hasn't already heard, accumulating up to 30.

Freshness: every release surfaced is remembered per-profile on disk, so each
new generation excludes previously-dug releases and returns new material.

Parallelism mirrors SampleHunter's _process_parallel: downloads + librosa run
in a thread pool; CLAP inference is serialized by a lock inside clap_model.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

import paths_boot  # noqa: F401  -- engine_libs on sys.path
from paths_boot import DATA_DIR

import discogs_ext
import listened_store
import yt_hunter
import features
import scorer

HIT_CLAP = 0.78          # raw CLAP cosine that maps to ~95% "vibe" (SampleHunter)
TARGET = 30              # playlist length
MAX_ROUNDS = 10          # safety ceiling
POOL_WORKERS = 4         # parallel download+analysis workers


def _seen_path(pid: str):
    return DATA_DIR / f"seen_releases_{pid}.json"


def _load_seen(pid: str) -> set:
    p = _seen_path(pid)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_seen(pid: str, ids: set):
    _seen_path(pid).write_text(json.dumps(sorted(ids)), encoding="utf-8")


def vibe_pct(clap_sim: float) -> int:
    return int(clap_sim / HIT_CLAP * 95)


def _evaluate(cand: dict, profile_emb: np.ndarray) -> dict | None:
    """Resolve -> download -> CLAP -> score one candidate. None on failure/skip."""
    yt = yt_hunter.search_yt_for_track(cand["artist"], cand["title"])
    if not yt:
        return None
    vid = yt["video_id"]
    if listened_store.contains_video(vid):
        return None
    snippet = yt_hunter.download_snippet(vid)
    feat = features.extract(snippet, with_clap=True, drum_robust=True, segment=True)
    clap = feat.get("clap")
    if clap is None:
        return None
    sim = scorer._cos(profile_emb, clap)
    vibe = vibe_pct(sim)
    return {
        "artist":       cand["artist"],
        "title":        cand["title"],
        "release_title": cand.get("release_title", ""),
        "year":         cand.get("year"),
        "label":        cand.get("label", ""),
        "country":      cand.get("country", ""),
        "discogs_id":   cand.get("discogs_id"),
        "rating_avg":   cand.get("rating_avg"),
        "cover_image":  cand.get("cover_image", ""),
        "video_id":     vid,
        "yt_title":     yt.get("title", ""),
        "clap":         round(float(sim), 3),
        "vibe":         vibe,
    }


def run(job: dict, profile: str, profile_emb: np.ndarray,
        vibe_gate: int = 55, max_have: int = 800,
        min_rating: float = 3.8, min_votes: int = 2,
        require_rating: bool = False, max_listeners: int = 400_000,
        target: int = TARGET):
    """Run a full generation, mutating `job` in place for progress polling."""
    seen_releases = _load_seen(profile)
    seen_videos: set = set()
    accepted: list[dict] = []

    job.update({"status": "running", "profile": profile, "target": target,
                "accepted": 0, "analyzed": 0, "results": [], "gate": vibe_gate})

    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            if job.get("stop_requested") or len(accepted) >= target:
                break
            job["status_msg"] = f"Round {rnd}: cerco dischi su Discogs…"

            cands, diag = discogs_ext.build_candidates(
                profile, max_have=max_have, min_rating=min_rating,
                min_votes=min_votes, require_rating=require_rating,
                max_listeners=max_listeners, exclude_ids=seen_releases,
            )
            seen_releases.update(diag.get("release_ids", []))

            # cheap pre-filter: drop already-heard tracks before any download
            cands = [c for c in cands
                     if not listened_store.contains(c["artist"], c["title"])]
            if not cands:
                job["status_msg"] = f"Round {rnd}: nessun candidato nuovo."
                continue

            with ThreadPoolExecutor(max_workers=POOL_WORKERS) as ex:
                futs = {}
                for c in cands:
                    if job.get("stop_requested") or len(accepted) >= target:
                        break
                    futs[ex.submit(_evaluate, c, profile_emb)] = c
                for fut in as_completed(futs):
                    job["analyzed"] = job.get("analyzed", 0) + 1
                    try:
                        res = fut.result()
                    except Exception as e:
                        job.setdefault("skipped", []).append(str(e)[:160])
                        res = None
                    if not res:
                        continue
                    if res["video_id"] in seen_videos:
                        continue
                    seen_videos.add(res["video_id"])
                    if res["vibe"] >= vibe_gate and len(accepted) < target:
                        accepted.append(res)
                        accepted.sort(key=lambda r: r["vibe"], reverse=True)
                        job["accepted"] = len(accepted)
                        job["results"] = accepted
                        job["progress"] = int(len(accepted) / target * 100)
                    job["status_msg"] = (
                        f"Round {rnd} · analizzati {job['analyzed']} · "
                        f"in playlist {len(accepted)}/{target}")

        _save_seen(profile, seen_releases)
        stopped = job.get("stop_requested", False)
        job["status"] = "stopped" if stopped else "done"
        job["progress"] = 100
        job["results"] = accepted
        job["accepted"] = len(accepted)
        job["status_msg"] = (
            ("🛑 Fermata" if stopped else "✓ Completata")
            + f" — {len(accepted)} brani · {job.get('analyzed', 0)} analizzati")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
