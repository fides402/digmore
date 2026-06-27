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
import asyncio
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from fastapi import FastAPI, Form, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, FileResponse, RedirectResponse
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
    vibe_gate:      int   = Form(0),
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

    import taste
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "status": "running", "progress": 0,
           "results": [], "accepted": 0, "analyzed": 0}
    JOBS[job_id] = job

    def _on_track(video_id: str):
        _url_executor.submit(_preload_track, video_id)

    threading.Thread(
        target=digger.run,
        kwargs=dict(job=job, profile=profile, profile_emb=emb,
                    vibe_gate=vibe_gate, max_have=max_have, min_rating=min_rating,
                    min_votes=min_votes, require_rating=require_rating,
                    max_listeners=max_listeners, target=target,
                    taste_tune=taste.is_active(),
                    on_track_accepted=_on_track),
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


# ── Library sync (cross-device) ──────────────────────────────────────────────
_LIB_FILE = Path(__file__).resolve().parent / "library.json"
_lib_lock = threading.Lock()


def _lib_read() -> list:
    with _lib_lock:
        try:
            return json.loads(_LIB_FILE.read_text(encoding="utf-8")) if _LIB_FILE.exists() else []
        except Exception:
            return []


_lib_ts: float = 0.0   # incremented on every write


def _lib_write(data: list):
    global _lib_ts
    with _lib_lock:
        _LIB_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        import time; _lib_ts = time.time()


@app.get("/api/library")
def library_get():
    return {"playlists": _lib_read(), "ts": _lib_ts}


@app.post("/api/library")
async def library_save(request: Request):
    pl = await request.json()
    if not pl.get("id") or not pl.get("name"):
        raise HTTPException(400, "id e name obbligatori")
    lib = _lib_read()
    lib = [p for p in lib if p.get("id") != pl["id"]]
    lib.insert(0, pl)
    _lib_write(lib)
    return {"ok": True}


@app.delete("/api/library/{pl_id}")
def library_delete(pl_id: str):
    lib = [p for p in _lib_read() if p.get("id") != pl_id]
    _lib_write(lib)
    return {"ok": True}


# ── Taste fine-tuning (locale, privato) ───────────────────────────────────────

@app.post("/api/listen")
async def listen_event(request: Request):
    """Registra un evento d'ascolto (solo brani effettivamente riprodotti)."""
    import taste
    b = await request.json()
    taste.log_event(b.get("video_id", ""),
                    float(b.get("listened_sec", 0) or 0),
                    float(b.get("duration_sec", 0) or 0),
                    profile=b.get("profile", ""))
    return {"ok": True}


@app.get("/api/taste/status")
def taste_status():
    import taste
    return taste.status()


@app.post("/api/taste/toggle")
async def taste_toggle(request: Request):
    import taste
    b = await request.json()
    return {"active": taste.set_active(bool(b.get("active", False)))}


@app.post("/api/taste/reset")
def taste_reset():
    import taste
    taste.reset()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/mark_unembeddable/{video_id}")
def mark_unembeddable(video_id: str):
    from engine_libs import yt_hunter, kvcache
    kvcache.put(yt_hunter._EMBED_NS, video_id, False)
    return {"ok": True}


# ── Audio streaming ────────────────────────────────────────────────────────────
# Architettura:
#  - /api/stream/{id}: estrae URL CDN audio via yt-dlp (cookie freschi + bgutil).
#  - Chiamato dal frontend come fallback quando l'IFrame YT dà errore 101/150.
#  - URL cachati 5h (TTL CDN YouTube).

import time as _time

_url_executor = ThreadPoolExecutor(max_workers=4)
_cdn_url_cache: dict[str, tuple[str, float]] = {}
_cdn_headers_cache: dict[str, dict] = {}
_CDN_TTL = 5 * 3600
_cdn_lock = threading.Lock()
_in_flight: set[str] = set()


def _extract_cdn_url(video_id: str) -> str:
    with _cdn_lock:
        cached = _cdn_url_cache.get(video_id)
        if cached and _time.time() < cached[1]:
            return cached[0]

    from engine_libs.yt_hunter import YoutubeDL
    opts = {
        "format": "140/139/bestaudio[ext=m4a]/bestaudio[has_drm=0]",
        "quiet": True, "no_warnings": True,
        "socket_timeout": 20,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False)
        # yt-dlp applica il format selector e mette la URL scelta in info["url"]
        url = info.get("url", "")
        _cdn_headers_cache[video_id] = dict(info.get("http_headers") or {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
    except Exception as e:
        print(f"[stream] fail {video_id}: {e}", flush=True)
        url = ""

    if not url:
        raise ValueError(f"no stream URL for {video_id}")

    print(f"[stream] ok {video_id}", flush=True)
    with _cdn_lock:
        _cdn_url_cache[video_id] = (url, _time.time() + _CDN_TTL)
    return url


def _preload_track(video_id: str):
    with _cdn_lock:
        if video_id in _in_flight:
            return
        cached = _cdn_url_cache.get(video_id)
        if cached and _time.time() < cached[1]:
            return
        _in_flight.add(video_id)
    try:
        _extract_cdn_url(video_id)
    except Exception as e:
        print(f"[preload] {video_id}: {e}", flush=True)
    finally:
        with _cdn_lock:
            _in_flight.discard(video_id)


@app.get("/api/stream/{video_id}")
async def stream_audio(video_id: str, request: Request):
    """Proxy audio CDN YouTube — evita CORS/403 del redirect diretto."""
    try:
        loop = asyncio.get_event_loop()
        cdn_url = await loop.run_in_executor(_url_executor, _extract_cdn_url, video_id)
    except Exception as e:
        print(f"[stream] ERROR {video_id}: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=502)

    # Proxy della richiesta con gli header originali di yt-dlp
    headers = dict(_cdn_headers_cache.get(video_id) or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    range_hdr = request.headers.get("range")
    if range_hdr:
        headers["Range"] = range_hdr

    import queue as _queue

    result_q: _queue.Queue = _queue.Queue()

    def _stream_gen(cdn_resp):
        for chunk in cdn_resp.iter_content(chunk_size=65536):
            if chunk:
                yield chunk
        cdn_resp.close()

    # Apriamo la connessione CDN in un thread (requests è sync)
    cdn_resp_holder: list = []
    cdn_err_holder: list = []

    def _open_cdn():
        try:
            r = requests.get(cdn_url, headers=headers, stream=True, timeout=30)
            cdn_resp_holder.append(r)
        except Exception as e:
            cdn_err_holder.append(e)

    import concurrent.futures as _cf
    fut = _cf.ThreadPoolExecutor(max_workers=1).submit(_open_cdn)
    fut.result(timeout=15)

    if cdn_err_holder:
        return JSONResponse({"error": str(cdn_err_holder[0])}, status_code=502)

    cdn_resp = cdn_resp_holder[0]
    content_type = cdn_resp.headers.get("Content-Type", "audio/mp4")
    resp_headers: dict = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    if "Content-Length" in cdn_resp.headers:
        resp_headers["Content-Length"] = cdn_resp.headers["Content-Length"]
    if "Content-Range" in cdn_resp.headers:
        resp_headers["Content-Range"] = cdn_resp.headers["Content-Range"]
    status_code = cdn_resp.status_code if cdn_resp.status_code in (200, 206) else 200

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        _stream_gen(cdn_resp),
        status_code=status_code,
        media_type=content_type,
        headers=resp_headers,
    )


@app.post("/api/preload/{video_id}")
async def preload_stream(video_id: str):
    """Lancia estrazione URL CDN in background (no-wait)."""
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_url_executor, _preload_track, video_id)
    return {"ok": True}


@app.post("/api/audio/{video_id}/delete")
def delete_audio(video_id: str):
    with _cdn_lock:
        _cdn_url_cache.pop(video_id, None)
    return {"ok": True}


@app.post("/api/audio/cache/clear")
def clear_audio_cache():
    with _cdn_lock:
        n = len(_cdn_url_cache)
        _cdn_url_cache.clear()
    print(f"[cache] cleared {n} CDN URLs", flush=True)
    return {"ok": True, "cleared": n}


_tunnel_url: str = ""

@app.post("/api/tunnel")
def set_tunnel(body: dict):
    global _tunnel_url
    _tunnel_url = body.get("url", "")
    return {"ok": True}

@app.get("/api/tunnel")
def get_tunnel():
    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "?"
    return {"tunnel": _tunnel_url, "local": f"http://{local_ip}:8099"}


# ── Static UI (local dev / single-host deploy) ───────────────────────────────
_WEB = (Path(__file__).resolve().parents[1] / "web")
if _WEB.is_dir():
    @app.get("/", include_in_schema=False)
    def root():
        idx = _WEB / "index.html"
        return FileResponse(str(idx)) if idx.exists() else JSONResponse({"ok": True})

    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
