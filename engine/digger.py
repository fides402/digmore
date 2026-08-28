"""Playlist orchestrator -- the heart of DIGMORE.

Candidate source: Discogs (obscure 1969-1983 releases) → resolve on YouTube
via ytsearch8 (this works on HF datacenter IPs unlike playlist scraping).
Score: CLAP cosine vs profile embedding, no hard gate.
Sort by vibe descending, return top N.
"""
import json
import os
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
import taste

HIT_CLAP     = 0.78
CLAP_MIN     = 0.01   # skip only silent/noise; show everything else
TARGET       = 30
MAX_ROUNDS   = 10
POOL_WORKERS = 4

# Per-candidate why-it-was-skipped tracing, off by default (silent no-op for
# the live local server). Set DIGGER_DEBUG=1 to see why every candidate was
# rejected — added after a GitHub Actions run came back "accepted=0,
# analyzed=266" with zero exceptions logged, i.e. every candidate was
# rejected by a normal `return None`, not a crash, and there was no visibility
# into which check was doing the rejecting.
_DEBUG = bool(os.environ.get("DIGGER_DEBUG"))


def _dbg(msg: str):
    if _DEBUG:
        print(f"[digger] {msg}", flush=True)

# ── Taste fine-tuning (additivo, capato) ──────────────────────────────────────
TASTE_LAMBDA   = 0.40   # peso max del profilo affinato sul ranking
TASTE_LAMBDA_N = 0.15   # peso repulsione zona negativa (< LAMBDA: asimmetrico)
TASTE_REP_CAP  = 0.10   # tetto assoluto della repulsione
TASTE_EXPLORE  = 0.15   # quota di risultati ranked sul solo P0 (anti-eco)


def _seen_path(pid: str) -> Path:
    return DATA_DIR / f"seen_releases_{pid}.json"

def _seen_videos_path(pid: str) -> Path:
    return DATA_DIR / f"seen_videos_{pid}.json"


def _load_seen(pid: str) -> set:
    p = _seen_path(pid)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _load_seen_videos(pid: str) -> set:
    p = _seen_videos_path(pid)
    if p.exists():
        try:
            return set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_seen(pid: str, ids: set):
    _seen_path(pid).write_text(json.dumps(sorted(ids)), encoding="utf-8")

def _save_seen_videos(pid: str, ids: set):
    _seen_videos_path(pid).write_text(json.dumps(sorted(ids)), encoding="utf-8")


def vibe_pct(clap_sim: float) -> int:
    return int(clap_sim / HIT_CLAP * 95)


def _clap_sim(profile_emb: np.ndarray, cand_clap) -> float:
    if cand_clap is None:
        return 0.0
    cand = np.asarray(cand_clap, dtype=np.float32)
    if cand.ndim == 2:
        return float(np.clip((cand @ profile_emb).max(), 0.0, 1.0))
    return float(np.clip(np.dot(profile_emb, cand), 0.0, 1.0))


_EVAL_TIMEOUT = 120  # hard per-candidate wall-clock budget (seconds)


def _evaluate_inner(cand: dict, profile_emb: np.ndarray, yt_module=None) -> dict | None:
    """Discogs candidate → find on YouTube → CLAP score. None on failure."""
    yt_mod = yt_module or yt_hunter
    tag = f"{cand['artist']} - {cand['title']}"
    yt = yt_mod.search_yt_for_track(cand["artist"], cand["title"])
    if not yt:
        _dbg(f"REJECT no_yt_match: {tag}")
        return None
    vid = yt["video_id"]
    if listened_store.contains_video(vid):
        _dbg(f"REJECT already_listened: {tag} ({vid})")
        return None
    snippet = yt_mod.download_snippet(vid)
    clap = clap_model.embed_audio(snippet)
    if clap is None:
        _dbg(f"REJECT clap_embed_none: {tag} ({vid})")
        return None
    sim = _clap_sim(profile_emb, clap)
    if sim < CLAP_MIN:
        _dbg(f"REJECT below_clap_min sim={sim:.4f}: {tag} ({vid})")
        return None
    _dbg(f"ACCEPT sim={sim:.4f}: {tag} ({vid})")
    # pooled+normalized vector for the taste centroid (same space as P0)
    vec = np.asarray(clap, dtype=np.float32)
    if vec.ndim == 2:
        vec = vec.mean(axis=0)
    vec = (vec / (np.linalg.norm(vec) + 1e-9)).astype(np.float32)
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
        "genres":        cand.get("genres", []),
        "styles":        cand.get("styles", []),
        "_vec":          vec.tolist(),   # stripped before sending to client
    }


def _public(r: dict) -> dict:
    """Copy of a result without private keys (underscore-prefixed)."""
    return {k: v for k, v in r.items() if not k.startswith("_")}


def _evaluate(cand: dict, profile_emb: np.ndarray, yt_module=None) -> dict | None:
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
            result.append(_evaluate_inner(cand, profile_emb, yt_module))
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
        target: int = TARGET, taste_tune: bool = False,
        on_track_accepted=None, yt_module=None, **_kw):
    seen_releases = _load_seen(profile)
    seen_videos: set = _load_seen_videos(profile)
    raw_results: list = []
    best_clap = 0.0

    # ── Taste fine-tuning: profilo affinato ancorato a P0 (se attivo) ──────────
    tuned_emb = None
    neg_emb = None
    taste_info = {"active": False}
    if taste_tune:
        try:
            tuned_emb, info = taste.tuned_profile(profile_emb, profile)
            if info.get("ready"):
                neg_emb = taste.negative_centroid(profile)
                taste_info = {"active": True, **info}
            else:
                tuned_emb = None          # non abbastanza segnale → nessun effetto
                taste_info = {"active": True, "ready": False, **info}
        except Exception as e:
            taste_info = {"active": True, "error": str(e)[:120]}

    def _score(r: dict) -> float:
        base = float(r["clap"])
        if tuned_emb is None or "_vec" not in r:
            return base
        ts = _clap_sim(tuned_emb, r["_vec"])
        s = (1.0 - TASTE_LAMBDA) * base + TASTE_LAMBDA * ts
        if neg_emb is not None:
            rep = max(0.0, _clap_sim(neg_emb, r["_vec"]))
            s -= min(TASTE_LAMBDA_N * rep, TASTE_REP_CAP)
        return s

    def _rank(results: list) -> list:
        """Top `target` by tuned score, con quota esplorazione e re-proba zone incerte."""
        if tuned_emb is None:
            return sorted(results, key=lambda r: r["clap"], reverse=True)[:target]

        ranked = sorted(results, key=_score, reverse=True)

        # quota esplorazione (anti-eco): ranked sul solo P0
        n_expl = max(1, round(target * TASTE_EXPLORE))
        main = ranked[:max(target - n_expl, 0)]
        main_ids = {r["video_id"] for r in main}
        explorers = sorted((r for r in results if r["video_id"] not in main_ids),
                           key=lambda r: r["clap"], reverse=True)[:n_expl]
        out = main + explorers
        out = sorted(out, key=_score, reverse=True)[:target]

        # ── Re-proba zone incerte ────────────────────────────────────────────
        # Se la zona negativa ha bassa confidenza (pochi skip), lasciamo passare
        # 1-2 candidati vicini a N per ri-testare se erano falsi negativi.
        # Se vengono ascoltati → il segnale negativo si indebolisce.
        # Se vengono skippati di nuovo → la confidenza cresce.
        if neg_emb is not None:
            conf = taste.neg_confidence(profile)
            if conf < 0.85:
                # slot di re-proba: 2 se molto incerto, 1 se mediamente incerto
                n_reproba = 2 if conf < 0.40 else 1
                out_ids = {r["video_id"] for r in out}
                # candidati vicini alla zona negativa (alta sim a N) con base decente
                reproba_pool = sorted(
                    (r for r in results if r["video_id"] not in out_ids and "_vec" in r),
                    key=lambda r: _clap_sim(neg_emb, r["_vec"]),
                    reverse=True
                )
                probes = [r for r in reproba_pool if float(r["clap"]) >= CLAP_MIN * 5][:n_reproba]
                if probes:
                    # sostituisce gli ultimi slot (quelli con score più basso)
                    out = out[:max(target - len(probes), 0)] + probes
                    out = sorted(out, key=_score, reverse=True)[:target]
                    taste_info["reproba"] = len(probes)
                    taste_info["neg_confidence"] = round(conf, 2)

        return out

    job.update({"status": "running", "profile": profile, "target": target,
                "accepted": 0, "analyzed": 0, "results": [],
                "best_clap_pct": 0, "taste": taste_info})

    # Auto-rilassamento progressivo: quando i round rendono pochi candidati,
    # allarghiamo gradualmente il rating gate e il tetto di notorietà (mai la
    # finestra anni). Sale di un livello per ogni round magro consecutivo.
    relax = 0
    lean_streak = 0

    try:
        for rnd in range(1, MAX_ROUNDS + 1):
            if job.get("stop_requested"):
                break
            if len(raw_results) >= target * 2:
                break

            # parametri effettivi in base al livello di rilassamento
            cur_min_rating = max(0.0, min_rating - 0.4 * relax)
            cur_max_listeners = int(max_listeners * (2 ** relax))
            cur_require_rating = require_rating and relax == 0

            rmsg = f" (ricerca allargata ×{relax})" if relax else ""
            job["status_msg"] = f"Round {rnd}: cerco dischi su Discogs…{rmsg}"

            cands, diag = discogs_ext.build_candidates(
                profile,
                max_have=max_have,
                min_rating=cur_min_rating,
                min_votes=min_votes,
                require_rating=cur_require_rating,
                max_listeners=cur_max_listeners,
                exclude_ids=seen_releases,
                n_releases=12,
                tracks_per_release=3,
            )
            seen_releases.update(diag.get("release_ids", []))

            cands = [c for c in cands
                     if not listened_store.contains(c["artist"], c["title"])]
            if len(cands) < 3:
                # round magro → sali di rilassamento per il prossimo giro
                lean_streak += 1
                if lean_streak >= 1 and relax < 4:
                    relax += 1
                if not cands:
                    job["status_msg"] = (
                        f"Round {rnd}: pochi dischi nuovi, allargo la ricerca…")
                    continue
            else:
                lean_streak = 0

            job["status_msg"] = (
                f"Round {rnd}: {len(cands)} tracce trovate su Discogs, "
                f"cerco su YouTube…"
            )

            with ThreadPoolExecutor(max_workers=POOL_WORKERS) as ex:
                futs = {ex.submit(_evaluate, c, profile_emb, yt_module): c for c in cands}
                for fut in as_completed(futs):
                    job["analyzed"] = job.get("analyzed", 0) + 1
                    try:
                        res = fut.result()
                    except Exception as e:
                        job.setdefault("skipped", []).append(str(e)[:160])
                        res = None

                    if res and res["video_id"] not in seen_videos and not listened_store.contains_video(res["video_id"]):
                        seen_videos.add(res["video_id"])
                        if float(res["clap"]) > best_clap:
                            best_clap = float(res["clap"])
                        # cache the candidate embedding for taste learning
                        try:
                            taste.cache_embedding(res["video_id"], res.get("_vec"))
                        except Exception:
                            pass
                        raw_results.append(res)
                        if on_track_accepted:
                            try: on_track_accepted(res)
                            except Exception: pass

                    best_pct = vibe_pct(best_clap)
                    job["best_clap_pct"] = best_pct
                    job["progress"] = min(best_pct, 95)
                    top_now = _rank(raw_results)
                    job["results"] = [_public(r) for r in top_now]
                    job["accepted"] = len(top_now)
                    job["status_msg"] = (
                        f"[Round {rnd}] analizzati {job['analyzed']} · "
                        f"trovati {len(raw_results)} · "
                        f"miglior vibe: {int(best_clap * 100)}%"
                    )

        _save_seen(profile, seen_releases)
        _save_seen_videos(profile, seen_videos)

        pool = raw_results
        if vibe_gate > 0:
            pool = [r for r in pool if r["vibe"] >= vibe_gate]
        final = _rank(pool)

        if final:
            top = max(_score(r) for r in final)
            for r in final:
                r["vibe"] = min(int(_score(r) / max(top, 0.01) * 95), 95)

        stopped = job.get("stop_requested", False)
        job["status"]   = "stopped" if stopped else "done"
        job["progress"] = 100
        job["results"]  = [_public(r) for r in final]
        job["accepted"] = len(final)
        job["status_msg"] = (
            ("Fermata" if stopped else "Completata")
            + f" — {len(final)} brani · {job.get('analyzed', 0)} analizzati · "
            + f"miglior vibe: {int(best_clap * 100)}%"
        )
    except Exception as e:
        job["status"] = "error"
        job["error"]  = str(e)
