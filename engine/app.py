"""DIGMORE engine -- FastAPI.

Hosts the profile builder + playlist digger. Designed to run on Hugging Face
Spaces (Docker) while the UI is served from Vercel; CORS is open so the Vercel
frontend can call it. For local dev it also serves the static UI from ../web.

Routes (all under /api):
  GET  /api/profiles                       -> profiles + build state
  POST /api/profiles/{pid}/build           -> async build embedding from CSV
  GET  /api/profiles/build-status/{job}    -> build progress
  POST /api/generate                       -> start a playlist generation
  GET  /api/generate/{job}                 -> generation progress + results
  POST /api/generate/{job}/stop            -> request stop
  POST /api/listened/import                -> add heard tracks (CSV/text/file)
  POST /api/listened/mark                  -> auto-mark a played track
  GET  /api/listened/stats                 -> counts
  GET  /api/cover?u=<discogs_url>          -> proxy a cover image (avoids hotlink/CORS)
"""
import os
import threading
import uuid
from pathlib import Path

import requests
from fastapi import FastAPI, Form, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles

import paths_boot  # noqa: F401  -- engine_libs on sys.path
import profiles
import digger
import listened_store
import discogs_hunter  # for the cover-proxy User-Agent header

app = FastAPI(title="DIGMORE engine")

_DEFAULT_CORS = (
    "https://digmore-app.vercel.app,"
    "https://digmore-app-*.vercel.app,"
    "*"   # allow all for local dev; restrict in prod via DIGMORE_CORS env
)
_origins = os.environ.get("DIGMORE_CORS", _DEFAULT_CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if "*" in _origins else [o.strip() for o in _origins.split(",")],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)

JOBS: dict[str, dict] = {}          # generation jobs
BUILD_JOBS: dict[str, dict] = {}    # profile-build jobs


# ── Profiles ─────────────────────────────────────────────────────────────────

@app.get("/api/profiles")
def get_profiles():
    return {"profiles": profiles.list_status()}


@app.post("/api/profiles/{pid}/build")
def build_profile(pid: str):
    if pid not in profiles.PROFILES:
        raise HTTPException(404, f"profilo sconosciuto: {pid}")
    if not profiles.csv_path(pid).exists():
        raise HTTPException(400, f"CSV mancante: metti {profiles.PROFILES[pid]['csv']} in profiles_csv/")
    running = [j for j in BUILD_JOBS.values()
               if j.get("pid") == pid and j.get("status") == "running"]
    if running:
        return {"ok": False, "reason": "build già in corso", "job_id": running[-1]["job_id"]}

    job_id = uuid.uuid4().hex[:10]
    job = {"job_id": job_id, "pid": pid, "status": "running", "progress": 0, "msg": "Avvio…"}
    BUILD_JOBS[job_id] = job

    def _run():
        def cb(done, total, msg):
            job["progress"] = int(done / max(total, 1) * 100)
            job["msg"] = msg
        try:
            profiles.build_embedding(pid, progress_cb=cb)
            job["status"] = "done"; job["progress"] = 100
        except Exception as e:
            job["status"] = "error"; job["msg"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/profiles/build-status/{job_id}")
def build_status(job_id: str):
    j = BUILD_JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return j


# ── Generation ───────────────────────────────────────────────────────────────

@app.post("/api/generate")
def generate(
    profile:        str   = Form(...),
    vibe_gate:      int   = Form(55),
    max_have:       int   = Form(800),
    min_rating:     float = Form(3.8),
    min_votes:      int   = Form(2),
    require_rating: bool  = Form(False),
    max_listeners:  int   = Form(400_000),
    target:         int   = Form(30),
):
    if profile not in profiles.PROFILES:
        raise HTTPException(404, f"profilo sconosciuto: {profile}")
    emb = profiles.get_embedding(profile)
    if emb is None:
        raise HTTPException(400, f"Profilo '{profile}' non ancora costruito — chiama /api/profiles/{profile}/build")

    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "status": "running", "progress": 0,
           "results": [], "accepted": 0, "analyzed": 0}
    JOBS[job_id] = job

    threading.Thread(
        target=digger.run,
        kwargs=dict(job=job, profile=profile, profile_emb=emb,
                    vibe_gate=vibe_gate, max_have=max_have, min_rating=min_rating,
                    min_votes=min_votes, require_rating=require_rating,
                    max_listeners=max_listeners, target=target),
        daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/generate/{job_id}")
def generate_status(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return j


@app.post("/api/generate/{job_id}/stop")
def generate_stop(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job sconosciuto")
    j["stop_requested"] = True
    return {"ok": True}


# ── Listened store ───────────────────────────────────────────────────────────

@app.post("/api/listened/import")
async def listened_import(text: str = Form(""), file: UploadFile | None = File(None)):
    payload = text or ""
    if file is not None:
        payload += "\n" + (await file.read()).decode("utf-8", errors="ignore")
    if not payload.strip():
        raise HTTPException(400, "nessun contenuto")
    return listened_store.import_csv(payload)


@app.post("/api/listened/mark")
def listened_mark(artist: str = Form(""), title: str = Form(""), video_id: str = Form("")):
    listened_store.add(artist, title, video_id)
    return listened_store.stats()


@app.get("/api/listened/stats")
def listened_stats():
    return listened_store.stats()


# ── Cover proxy ──────────────────────────────────────────────────────────────

@app.get("/api/cover")
def cover(u: str):
    """Proxy a Discogs cover image so the browser doesn't hit hotlink/CORS walls."""
    if not (u.startswith("https://i.discogs.com/") or u.startswith("https://img.discogs.com/")
            or u.startswith("https://i.ytimg.com/")):
        raise HTTPException(400, "url non consentito")
    try:
        r = requests.get(u, headers={"User-Agent": discogs_hunter.HEADERS["User-Agent"]},
                         timeout=12)
        r.raise_for_status()
    except requests.RequestException:
        raise HTTPException(502, "cover non recuperabile")
    return Response(content=r.content,
                    media_type=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/health")
def health():
    return {"ok": True}


# ── Static UI (local dev / single-host deploy) ───────────────────────────────
_WEB = (Path(__file__).resolve().parents[1] / "web")
if _WEB.is_dir():
    @app.get("/", include_in_schema=False)
    def root():
        idx = _WEB / "index.html"
        return FileResponse(str(idx)) if idx.exists() else JSONResponse({"ok": True})

    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
