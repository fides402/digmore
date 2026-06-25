"""Playlist orchestrator -- the heart of DIGMORE.

Mirrors SampleHunter's _run_search_discogs approach:
  - Pull random lesser-known, well-rated Discogs releases (1969-1983)
  - Resolve each track on YouTube, extract CLAP embedding
  - Score all candidates against the profile (no hard gate)
  - Sort by vibe% descending, return top N
  - batch_normalize at the end so the best track is always ~95%

Freshness: every release surfaced is remembered per-profile on disk.
Parallelism mirrors SampleHunter's _process_parallel.
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
import clap_model

HIT_CLAP    = 0.78    # raw CLAP cosine that maps to ~95% vibe (SampleHunter)
CLAP_MIN    = 0.10    # pre-filter: skip truly unrelated audio (same as SampleHunter)
TARGET      = 30
MAX_ROUNDS  = 10
POOL_WORKERS = 6      # downloads run in parallel; CLAP is lock-serialized


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


def _clap_sim(profile_emb: np.ndarray, cand_clap) -> float:
    """Cosine between profile (1-D) and candidate CLAP (1-D or 2-D segments).
    Identical to SampleHunter's _clap_sim."""
    if cand_clap is None:
        return 0.0
    cand = np.asarray(cand_clap, dtype=np.float32)
    if cand.ndim == 2:
        return float(np.clip((cand @ profile_emb).max(), 0.0, 1.0))
    return float(np.clip(np.dot(profile_emb, cand), 0.0, 1.0))


def _evaluate(cand: dict, profile_emb: np.ndarray) -> dict | None:
    """Resolve -> download short snippet -> CLAP -> score one candidate.

    CLAP-only fast path: DIGMORE scores purely on the CLAP cosine vs the
    profile, so we skip all the heavy librosa analysis (HPSS, mfcc, chroma,
    tempogram, key detection) that features.extract would compute and discard.
    """
    yt = yt_hunter.search_yt_for_track(cand["artist"], cand["title"])
    if not yt:
        return None
    vid = yt["video_id"]
    if listened_store.contains_video(vid):
        return None
    snippet = yt_hunter.download_snippet(vid)
    clap = clap_model.embed_audio(snippet)   # 512-dim L2-normalized, single window
    if clap is None:
        return None
    sim = _clap_sim(profile_emb, clap)
    # pre-filter: skip truly unrelated audio (same threshold as SampleHunter)
    if sim < CLAP_MIN:
        return None
    vibe = vibe_pct(sim)
    return {
        "artist":        cand["artist"],
        "title":         cand["title"],
        "release_title": cand.get("release_title", ""),
        "year":          cand.get("year"),
        "label":         cand.get("label", ""),
        "country":       cand.get("country", ""),
        "discogs_id":    cand.get("discogs_id"),
        "rating_avg":    cand.get("rating_avg"),
        "cover_image":   cand.get("cover_image", ""),
        "video_id":      vid,
        "yt_title":      yt.get("title", ""),
        "clap":          round(float(sim), 3),
        "vibe":          vibe,
    }


def run(job: dict, profile: str, profile_emb: np.ndarray,
        vibe_gate: int = 0, max_have: int = 800,
        min_rating: float = 3.8, min_votes: int = 2,
        require_rating: bool = False, max_listeners: int = 400_000,
        target: int = TARGET):
    """Run a full generation, mutating `job` in place for progress polling.

    Mirrors SampleHunter: collect all scored results, sort by vibe, return top N.
    vibe_gate is kept as a parameter but defaults to 0 (no filtering).
    """
    seen_releases = _load_seen(profile)
    seen_videos: set = set()
    raw_results: list = []
    best_clap = 0.0

    job.update({"status": "running", "profile": profile, "target": target,
                "accepted": 0, "analyzed": 0, "results": [],
                "best_clap_pct": 0, "gate": vibe_gate})

    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            if job.get("stop_requested"):
                break
            # Stop early once we have enough candidates (2× target for sorting)
            if len(raw_results) >= target * 2:
                break

            job["status_msg"] = f"Round {rnd}: cerco dischi su Discogs…"

            # Pull ~2.5× target candidates per round so a single round usually
            # suffices; fewer releases/tracks = fewer Discogs + YouTube calls.
            n_rel = max(8, min(16, target))
            cands, diag = discogs_ext.build_candidates(
                profile, max_have=max_have, min_rating=min_rating,
                min_votes=min_votes, require_rating=require_rating,
                max_listeners=max_listeners, exclude_ids=seen_releases,
                n_releases=n_rel, tracks_per_release=2,
            )
            seen_releases.update(diag.get("release_ids", []))

            cands = [c for c in cands
                     if not listened_store.contains(c["artist"], c["title"])]
            if not cands:
                job["status_msg"] = f"Round {rnd}: nessun candidato nuovo."
                continue

            with ThreadPoolExecutor(max_workers=POOL_WORKERS) as ex:
                futs = {}
                for c in cands:
                    if job.get("stop_requested"):
                        break
                    futs[ex.submit(_evaluate, c, profile_emb)] = c

                for fut in as_completed(futs):
                    job["analyzed"] = job.get("analyzed", 0) + 1
                    try:
                        res = fut.result()
                    except Exception as e:
                        job.setdefault("skipped", []).append(str(e)[:160])
                        res = None

                    if res and res["video_id"] not in seen_videos:
                        seen_videos.add(res["video_id"])
                        raw_clap = float(res.get("clap", 0))
                        if raw_clap > best_clap:
                            best_clap = raw_clap
                        raw_results.append(res)

                    best_pct = vibe_pct(best_clap)
                    job["best_clap_pct"] = best_pct
                    # Live partial results: top N sorted by vibe
                    top_now = sorted(raw_results,
                                     key=lambda r: r["clap"], reverse=True)[:target]
                    job["results"] = top_now
                    job["accepted"] = len(top_now)
                    job["progress"] = min(best_pct, 95)
                    job["status_msg"] = (
                        f"[Round {rnd}] Analizzati {job['analyzed']} · "
                        f"Miglior vibe: {int(best_clap * 100)}% "
                        f"(target {int(HIT_CLAP * 100)}%)"
                    )

        _save_seen(profile, seen_releases)

        # Final: sort, normalize so best = 95%, apply optional gate, cap at target
        final = sorted(raw_results, key=lambda r: r["clap"], reverse=True)
        if vibe_gate > 0:
            final = [r for r in final if r["vibe"] >= vibe_gate]
        final = final[:target]

        # Rescale vibe so the best track shows ~95% (matches SampleHunter)
        if final:
            top_clap = final[0]["clap"]
            scale = HIT_CLAP / max(top_clap, 0.01)
            for r in final:
                r["vibe"] = min(int(r["clap"] / max(top_clap, 0.01) * 95), 95)

        stopped = job.get("stop_requested", False)
        job["status"] = "stopped" if stopped else "done"
        job["progress"] = 100
        job["results"] = final
        job["accepted"] = len(final)
        job["status_msg"] = (
            ("Fermata" if stopped else "Completata")
            + f" — {len(final)} brani · {job.get('analyzed', 0)} analizzati · "
            + f"miglior vibe: {int(best_clap * 100)}%"
        )
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
