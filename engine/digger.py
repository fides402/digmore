"""Playlist orchestrator -- the heart of DIGMORE.

Candidate source: Discogs (obscure 1969-1983 releases) → resolve on YouTube
via ytsearch8 (this works on HF datacenter IPs unlike playlist scraping).
Score: CLAP cosine vs profile embedding, no hard gate.
Sort by vibe descending, return top N.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

import paths_boot  # noqa: F401
from paths_boot import DATA_DIR

import discogs_ext
import listened_store
import yt_hunter
import clap_model

HIT_CLAP     = 0.78
CLAP_MIN     = 0.05   # skip only silent/noise; show everything else
TARGET       = 30
MAX_ROUNDS   = 10
POOL_WORKERS = 4


def _seen_path(pid: str) -> Path:
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
    if cand_clap is None:
        return 0.0
    cand = np.asarray(cand_clap, dtype=np.float32)
    if cand.ndim == 2:
        return float(np.clip((cand @ profile_emb).max(), 0.0, 1.0))
    return float(np.clip(np.dot(profile_emb, cand), 0.0, 1.0))


_EVAL_TIMEOUT = 50   # hard per-candidate wall-clock budget (seconds)


def _evaluate_inner(cand: dict, profile_emb: np.ndarray) -> dict | None:
    """Discogs candidate → find on YouTube → CLAP score. None on failure."""
    yt = yt_hunter.search_yt_for_track(cand["artist"], cand["title"])
    if not yt:
        return None
    vid = yt["video_id"]
    if listened_store.contains_video(vid):
        return None
    snippet = yt_hunter.download_snippet(vid)
    clap = clap_model.embed_audio(snippet)
    if clap is None:
        return None
    sim = _clap_sim(profile_emb, clap)
    if sim < CLAP_MIN:
        return None
    return {
        "artist":        cand["artist"],
        "title":         cand["title"],
        "release_title": cand.get("release_title", ""),
        "year":          cand.get("year"),
        "label":         cand.get("label", ""),
        "country":       cand.get("country", ""),
        "discogs_id":    cand.get("discogs_id"),
        "rating_avg":    cand.get("rating_avg"),
        "cover_image":   cand.get("cover_image") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "video_id":      vid,
        "yt_title":      yt.get("title", ""),
        "clap":          round(float(sim), 3),
        "vibe":          vibe_pct(sim),
    }


def _evaluate(cand: dict, profile_emb: np.ndarray) -> dict | None:
    """_evaluate_inner with a hard wall-clock timeout.

    search_yt_for_track can hang on HF datacenter IPs when YouTube returns
    a bot-check page that yt-dlp tries to parse indefinitely despite
    socket_timeout. We wrap the whole call in a daemon thread so stuck
    workers don't freeze the pool forever.
    """
    result: list = []
    exc: list = []

    def _run():
        try:
            result.append(_evaluate_inner(cand, profile_emb))
        except Exception as e:
            exc.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=_EVAL_TIMEOUT)
    if t.is_alive():
        raise TimeoutError(
            f"timeout ({_EVAL_TIMEOUT}s): {cand['artist']} – {cand['title']}")
    if exc:
        raise exc[0]
    return result[0] if result else None


def run(job: dict, profile: str, profile_emb: np.ndarray,
        vibe_gate: int = 0, max_have: int = 800,
        min_rating: float = 3.5, min_votes: int = 1,
        require_rating: bool = False, max_listeners: int = 500_000,
        target: int = TARGET, **_kw):
    seen_releases = _load_seen(profile)
    seen_videos: set = set()
    raw_results: list = []
    best_clap = 0.0

    job.update({"status": "running", "profile": profile, "target": target,
                "accepted": 0, "analyzed": 0, "results": [],
                "best_clap_pct": 0})

    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            if job.get("stop_requested"):
                break
            if len(raw_results) >= target * 2:
                break

            job["status_msg"] = f"Round {rnd}: cerco dischi su Discogs…"

            cands, diag = discogs_ext.build_candidates(
                profile,
                max_have=max_have,
                min_rating=min_rating,
                min_votes=min_votes,
                require_rating=require_rating,
                max_listeners=max_listeners,
                exclude_ids=seen_releases,
                n_releases=12,
                tracks_per_release=3,
            )
            seen_releases.update(diag.get("release_ids", []))

            cands = [c for c in cands
                     if not listened_store.contains(c["artist"], c["title"])]
            if not cands:
                job["status_msg"] = f"Round {rnd}: nessun candidato nuovo."
                continue

            job["status_msg"] = (
                f"Round {rnd}: {len(cands)} tracce trovate su Discogs, "
                f"cerco su YouTube…"
            )

            with ThreadPoolExecutor(max_workers=POOL_WORKERS) as ex:
                futs = {ex.submit(_evaluate, c, profile_emb): c for c in cands}
                for fut in as_completed(futs):
                    job["analyzed"] = job.get("analyzed", 0) + 1
                    try:
                        res = fut.result()
                    except Exception as e:
                        job.setdefault("skipped", []).append(str(e)[:160])
                        res = None

                    if res and res["video_id"] not in seen_videos:
                        seen_videos.add(res["video_id"])
                        if float(res["clap"]) > best_clap:
                            best_clap = float(res["clap"])
                        raw_results.append(res)

                    best_pct = vibe_pct(best_clap)
                    job["best_clap_pct"] = best_pct
                    job["progress"] = min(best_pct, 95)
                    top_now = sorted(raw_results,
                                     key=lambda r: r["clap"], reverse=True)[:target]
                    job["results"] = top_now
                    job["accepted"] = len(top_now)
                    job["status_msg"] = (
                        f"[Round {rnd}] analizzati {job['analyzed']} · "
                        f"trovati {len(raw_results)} · "
                        f"miglior vibe: {int(best_clap * 100)}%"
                    )

        _save_seen(profile, seen_releases)

        final = sorted(raw_results, key=lambda r: r["clap"], reverse=True)
        if vibe_gate > 0:
            final = [r for r in final if r["vibe"] >= vibe_gate]
        final = final[:target]

        if final:
            top_clap = final[0]["clap"]
            for r in final:
                r["vibe"] = min(int(r["clap"] / max(top_clap, 0.01) * 95), 95)

        stopped = job.get("stop_requested", False)
        job["status"]   = "stopped" if stopped else "done"
        job["progress"] = 100
        job["results"]  = final
        job["accepted"] = len(final)
        job["status_msg"] = (
            ("Fermata" if stopped else "Completata")
            + f" — {len(final)} brani · {job.get('analyzed', 0)} analizzati · "
            + f"miglior vibe: {int(best_clap * 100)}%"
        )
    except Exception as e:
        job["status"] = "error"
        job["error"]  = str(e)
