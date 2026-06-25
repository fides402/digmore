"""Playlist orchestrator -- the heart of DIGMORE.

Mirrors SampleHunter's _run_search (YT-playlist mode) exactly:
  - Search YouTube for genre playlists → guaranteed tracks that exist online
  - Download short snippet → CLAP embed → cosine vs profile
  - Collect all above CLAP_MIN, sort by vibe descending, return top N
  - Rescale so the best track = 95% vibe

Freshness: seen video_ids are remembered per-profile on disk.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

import paths_boot  # noqa: F401  -- engine_libs on sys.path
from paths_boot import DATA_DIR

import listened_store
import yt_hunter
import clap_model

HIT_CLAP     = 0.78   # cosine that maps to ~95% vibe (SampleHunter constant)
CLAP_MIN     = 0.10   # skip truly unrelated audio
TARGET       = 30
MAX_ROUNDS   = 10
BATCH_SIZE   = 20     # YT candidates per round (same as SampleHunter _BATCH_YT)
POOL_WORKERS = 4

# Profile id → YouTube search keyword (what the playlist search uses)
_GENRE_KW = {
    "soul": "soul",
    "jazz": "jazz",
    "ost":  "film score",
}


def _seen_path(pid: str) -> Path:
    return DATA_DIR / f"seen_videos_{pid}.json"


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


def _score_yt(yt: dict, profile_emb: np.ndarray) -> dict | None:
    """Download short snippet, CLAP-embed, score against profile. None = skip."""
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
        "artist":      yt.get("channel", ""),
        "title":       yt.get("title", ""),
        "video_id":    vid,
        "yt_title":    yt.get("title", ""),
        "cover_image": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "url":         yt.get("url", f"https://www.youtube.com/watch?v={vid}"),
        "clap":        round(float(sim), 3),
        "vibe":        vibe_pct(sim),
    }


def run(job: dict, profile: str, profile_emb: np.ndarray,
        vibe_gate: int = 0, target: int = TARGET, **_kwargs):
    """Run a full generation, mutating `job` in place for progress polling."""
    genre_kw = _GENRE_KW.get(profile, profile)
    seen_vids = _load_seen(profile)
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

            job["status_msg"] = f"Round {rnd}: cerco su YouTube ({genre_kw})…"

            # Search YouTube genre playlists — same as SampleHunter _run_search
            pool = yt_hunter.search_candidates(genre_kw, n=BATCH_SIZE * 3,
                                               exclude=seen_vids)
            batch = [c for c in pool if c["video_id"] not in seen_vids][:BATCH_SIZE]

            if not batch:
                job["status_msg"] = f"Round {rnd}: nessun nuovo candidato."
                continue

            with ThreadPoolExecutor(max_workers=POOL_WORKERS) as ex:
                futs = {}
                for c in batch:
                    if job.get("stop_requested"):
                        break
                    seen_vids.add(c["video_id"])
                    futs[ex.submit(_score_yt, c, profile_emb)] = c

                for fut in as_completed(futs):
                    job["analyzed"] = job.get("analyzed", 0) + 1
                    try:
                        res = fut.result()
                    except Exception as e:
                        job.setdefault("skipped", []).append(str(e)[:160])
                        res = None

                    if res:
                        raw_clap = float(res["clap"])
                        if raw_clap > best_clap:
                            best_clap = raw_clap
                        raw_results.append(res)

                    best_pct = vibe_pct(best_clap)
                    job["best_clap_pct"] = best_pct
                    job["progress"] = min(best_pct, 95)
                    top_now = sorted(raw_results,
                                     key=lambda r: r["clap"], reverse=True)[:target]
                    job["results"] = top_now
                    job["accepted"] = len(top_now)
                    job["status_msg"] = (
                        f"[Round {rnd}] Analizzati {job['analyzed']} · "
                        f"Miglior vibe: {int(best_clap * 100)}% "
                        f"(target {int(HIT_CLAP * 100)}%)"
                    )

        _save_seen(profile, seen_vids)

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
