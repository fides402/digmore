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
import seq_features
import sequencer

_OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "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")

# Modelli free — lista di fallback statica, sovrascritta al primo fetch da OR
_OR_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "qwen/qwen3-235b-a22b:free",
    "deepseek/deepseek-r1:free",
]
_or_models_ts: float = 0.0
_OR_MODELS_TTL = 6 * 3600   # ricarica ogni 6h


def _fetch_free_models() -> list[dict]:
    """Recupera da OpenRouter la lista aggiornata dei modelli free."""
    import time as _t
    global _OR_MODELS, _or_models_ts
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {_OPENROUTER_KEY}"},
            timeout=15,
        )
        r.raise_for_status()
        all_models = r.json().get("data", [])
        free = [
            m for m in all_models
            if str(m.get("pricing", {}).get("prompt", "1")) == "0"
            and str(m.get("pricing", {}).get("completion", "1")) == "0"
            and m.get("id", "").endswith(":free")
        ]
        free.sort(key=lambda m: m.get("name", ""))
        if free:
            _OR_MODELS = [m["id"] for m in free]
            _or_models_ts = _t.time()
            print(f"[OR models] {len(free)} modelli free", flush=True)
        return free
    except Exception as e:
        print(f"[OR models] fetch fallito: {e}", flush=True)
        return []


def _ensure_models_fresh():
    import time as _t
    if _t.time() - _or_models_ts > _OR_MODELS_TTL:
        threading.Thread(target=_fetch_free_models, daemon=True).start()


# Carica la lista al boot in background
threading.Thread(target=_fetch_free_models, daemon=True).start()

# Una sola chiamata OR alla volta — il rate limit è per chiave, non per modello
_or_sem = threading.Semaphore(1)

def _or_call(messages: list, temperature: float = 0.5, max_tokens: int = 400, models: list | None = None) -> str:
    """Chiama OpenRouter con fallback tra modelli free e backoff su 429."""
    import time as _t
    with _or_sem:
        for attempt, model in enumerate(models or _OR_MODELS):
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {_OPENROUTER_KEY}",
                        "HTTP-Referer": "http://localhost:8099",
                        "X-Title": "DIGMORE",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, "messages": messages,
                          "temperature": temperature, "max_tokens": max_tokens},
                    timeout=40,
                )
                if resp.status_code == 429:
                    wait = 20 + attempt * 10   # 20s, 30s, 40s, 50s
                    print(f"[OR] 429 su {model}, aspetto {wait}s", flush=True)
                    _t.sleep(wait)
                    continue
                if resp.status_code in (502, 503):
                    print(f"[OR] {resp.status_code} su {model}, provo prossimo", flush=True)
                    _t.sleep(3)
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                print(f"[OR] ok {model}", flush=True)
                return content
            except Exception as e:
                print(f"[OR] errore {model}: {e}", flush=True)
                _t.sleep(3)
                continue
    raise RuntimeError("OpenRouter non disponibile — riprova tra qualche minuto")


_annot_cache: dict[str, dict] = {}   # video_id → {mood, feel, instruments, scene}
_annot_lock = threading.Lock()


import queue as _queue

_annot_queue: _queue.Queue = _queue.Queue()
_ANNOT_BATCH = 5       # brani per chiamata LLM
_ANNOT_INTERVAL = 25   # secondi tra un batch e l'altro (rate-limit friendly)


def _annotate_batch(tracks: list):
    """Annota fino a _ANNOT_BATCH brani in una sola chiamata LLM."""
    lines = []
    for i, t in enumerate(tracks):
        gs = " | ".join(filter(None, [
            ", ".join(t.get("genres") or []),
            ", ".join(t.get("styles") or []),
        ])) or "—"
        lines.append(
            f"{i}: {t.get('artist','?')} — {t.get('title','?')} "
            f"({t.get('year','?')}) [{t.get('label','?')}] [{gs}]"
        )
    prompt = (
        "Analizza questi brani musicali e ritorna SOLO un JSON array "
        "(nessun testo fuori dal JSON):\n"
        + "\n".join(lines) + "\n\n"
        'Formato: [{"i":0,"mood":["tag1","tag2"],"feel":["tag1","tag2"],'
        '"instruments":["str1","str2"],"scene":"frase evocativa 5-8 parole italiano"},...]\n'
        "mood=emozione/atmosfera. feel=texture sonora. instruments=strumenti principali. "
        "2-4 tag per campo. Tutti i brani dell'input."
    )
    try:
        content = _or_call(
            [{"role": "user", "content": prompt}], temperature=0.3,
            max_tokens=_ANNOT_BATCH * 120,
        )
        m = _re.search(r'\[.*\]', content, _re.DOTALL)
        if not m:
            return
        results = json.loads(m.group())
        for item in results:
            idx = item.get("i")
            if not isinstance(idx, int) or idx >= len(tracks):
                continue
            t = tracks[idx]
            vid = t.get("video_id", "")
            if not vid:
                continue
            tags = {
                "mood":        item.get("mood", []),
                "feel":        item.get("feel", []),
                "instruments": item.get("instruments", []),
                "scene":       item.get("scene", ""),
            }
            with _annot_lock:
                _annot_cache[vid] = tags
                t.update(tags)
            print(f"[annot] {vid} — {tags['scene']}", flush=True)
    except Exception as e:
        print(f"[annot batch] fail: {e}", flush=True)


def _annot_worker():
    """Thread unico che drena la coda a batch, con pausa tra un batch e l'altro."""
    import time as _t
    while True:
        batch = []
        try:
            batch.append(_annot_queue.get(timeout=10))
        except _queue.Empty:
            continue
        while len(batch) < _ANNOT_BATCH:
            try:
                batch.append(_annot_queue.get_nowait())
            except _queue.Empty:
                break
        with _annot_lock:
            to_do = [t for t in batch if t.get("video_id") not in _annot_cache]
        if to_do:
            _annotate_batch(to_do)
            _t.sleep(_ANNOT_INTERVAL)


threading.Thread(target=_annot_worker, daemon=True, name="annot-worker").start()


def _prefilter_tracks(tracks: list, message: str, max_n: int = 60) -> list:
    """Keyword scoring: seleziona i max_n brani più rilevanti per la richiesta."""
    import re as _re2
    msg_lower = message.lower()
    stopwords = {
        "di","il","la","lo","le","un","una","per","che","e","a","in","da","con",
        "su","non","ho","mi","voglio","dammi","qualcosa","musica","brani","playlist",
        "vorrei","metti","cerca","trova","seleziona","mood","feel","scena","momento",
    }
    keywords = {w for w in _re2.findall(r'\w+', msg_lower)
                if len(w) > 2 and w not in stopwords}

    def _score(t):
        text = " ".join(filter(None, [
            " ".join(t.get("genres") or []),
            " ".join(t.get("styles") or []),
            " ".join(t.get("mood") or []),
            " ".join(t.get("feel") or []),
            " ".join(t.get("instruments") or []),
            t.get("scene") or "",
            t.get("artist") or "",
            t.get("label") or "",
        ])).lower()
        kw = sum(1 for kw in keywords if kw in text)
        return kw * 3 + (t.get("vibe") or 0) / 100

    ranked = sorted(tracks, key=_score, reverse=True)
    # garantisce sempre una quota top-vibe per varietà
    top_vibe = sorted(tracks, key=lambda t: t.get("vibe") or 0, reverse=True)[:15]
    merged = {t["video_id"]: t for t in ranked[:max_n] + top_vibe if t.get("video_id")}
    return list(merged.values())[:max_n]

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
SEQUENCE_JOBS: dict[str, dict] = {}


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

    def _on_track(track: dict):
        vid = track.get("video_id", "")
        if vid:
            _url_executor.submit(_preload_track, vid)
        _annot_queue.put(track)

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
    with _annot_lock:
        cached = dict(_annot_cache)
    results = []
    for t in (j.get("results") or []):
        ann = cached.get(t.get("video_id", ""))
        if ann:
            t = {**t, **ann}
        results.append(t)
    return {**j, "results": results}


@app.post("/api/generate/{job_id}/stop")
def generate_stop(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job sconosciuto")
    j["stop_requested"] = True
    return {"ok": True}


# ── Sequenced TXT export ────────────────────────────────────────────────────

@app.post("/api/sequence")
async def start_sequence(request: Request):
    body = await request.json()
    tracks = body.get("tracks") or []
    if not isinstance(tracks, list) or not tracks:
        raise HTTPException(400, "playlist vuota")
    if len(tracks) > 60:
        raise HTTPException(400, "massimo 60 brani")
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "status": "running", "analyzed": 0,
           "total": len(tracks), "order": [], "transitions": [],
           "unanalyzed": [], "sequenced": False, "txt": None, "error": None,
           "name": str(body.get("name") or "playlist")}
    SEQUENCE_JOBS[job_id] = job
    while len(SEQUENCE_JOBS) > 20:
        oldest = next(iter(SEQUENCE_JOBS))
        if oldest == job_id: break
        SEQUENCE_JOBS.pop(oldest, None)

    def _run():
        try:
            feature_map = {}
            reanalyze = bool(body.get("reanalyze", False))
            def analyze(track):
                vid = str(track.get("video_id") or "")
                return vid, seq_features.features_for(vid, reanalyze=reanalyze) if vid else None
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(analyze, t) for t in tracks]
                for future in futures:
                    vid, feat = future.result()
                    if vid and feat is not None: feature_map[vid] = feat
                    job["analyzed"] += 1
            result = sequencer.sequence(tracks, feature_map)
            job.update(result)
            job["txt"] = sequencer.render_txt(result["order"])
            job["status"] = "done"
        except Exception as exc:
            job["status"] = "error"; job["error"] = str(exc)
    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/sequence/{job_id}")
def sequence_status(job_id: str):
    job = SEQUENCE_JOBS.get(job_id)
    if not job: raise HTTPException(404, "job sconosciuto")
    return job


@app.get("/api/sequence/{job_id}/txt")
def sequence_txt(job_id: str):
    job = SEQUENCE_JOBS.get(job_id)
    if not job: raise HTTPException(404, "job sconosciuto")
    if job.get("status") != "done": raise HTTPException(409, "export non pronto")
    filename = sequencer.slugify(job.get("name")) + ".txt"
    return Response(job.get("txt") or "", media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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


# ── OpenRouter model list ─────────────────────────────────────────────────────

@app.get("/api/or-models")
def or_models():
    _ensure_models_fresh()
    import time as _t
    all_models = []
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {_OPENROUTER_KEY}"},
            timeout=15,
        )
        r.raise_for_status()
        all_models = r.json().get("data", [])
    except Exception:
        pass
    free = [
        {"id": m["id"], "name": m.get("name", m["id"])}
        for m in all_models
        if str(m.get("pricing", {}).get("prompt", "1")) == "0"
        and str(m.get("pricing", {}).get("completion", "1")) == "0"
        and m.get("id", "").endswith(":free")
    ]
    free.sort(key=lambda m: m["name"])
    if not free:
        free = [{"id": m, "name": m} for m in _OR_MODELS]
    return {"models": free}


# ── DigChat ──────────────────────────────────────────────────────────────────

import re as _re
import datetime as _dt


@app.post("/api/digchat")
async def digchat(request: Request):
    body = await request.json()
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    tracks = body.get("tracks") or []
    preferred_model = body.get("model") or ""

    if not message:
        raise HTTPException(400, "message vuoto")
    if not tracks:
        raise HTTPException(400, "nessun brano disponibile — genera prima una playlist")

    tracks = _prefilter_tracks(tracks, message)

    catalog_lines = []
    for i, t in enumerate(tracks):
        genres = ", ".join(t.get("genres") or [])
        styles = ", ".join(t.get("styles") or [])
        genre_str = " | ".join(filter(None, [genres, styles])) or "—"
        mood_str = ", ".join(t.get("mood") or [])
        feel_str = ", ".join(t.get("feel") or [])
        instr_str = ", ".join(t.get("instruments") or [])
        scene_str = t.get("scene") or ""
        ann_parts = " | ".join(filter(None, [
            f"mood:{mood_str}" if mood_str else "",
            f"feel:{feel_str}" if feel_str else "",
            f"instr:{instr_str}" if instr_str else "",
            f'"{scene_str}"' if scene_str else "",
        ]))
        catalog_lines.append(
            f"{i}: {t.get('artist','?')} — {t.get('title','?')} "
            f"({t.get('year','?')}) [{t.get('label','?')}] "
            f"[{genre_str}] vibe:{t.get('vibe',0)}%"
            + (f" | {ann_parts}" if ann_parts else "")
        )
    catalog = "\n".join(catalog_lines)

    system_prompt = (
        "Sei DigChat, l'assistente musicale di DIGMORE. "
        "Hai accesso a un catalogo di dischi obscuri 1969-1983 trovati dal digger. "
        "Il vibe% indica quanto ogni brano corrisponde al profilo musicale dell'utente (CLAP similarity).\n\n"
        f"CATALOGO ({len(tracks)} brani):\n{catalog}\n\n"
        "Il tuo compito: selezionare i brani più adatti alla richiesta (5-30 brani), "
        "scegliere un nome tematico evocativo per la playlist, rispondere brevemente in italiano.\n"
        "Se l'utente chiede varietà o una seconda selezione, scegli brani DIVERSI dalla conversazione precedente.\n\n"
        "Rispondi SOLO con JSON valido (nessun testo fuori dal JSON):\n"
        '{"reply":"risposta breve 1-2 frasi","playlist_name":"Nome Evocativo","track_indices":[0,5,12]}'
    )

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    try:
        models = ([preferred_model] + [m for m in _OR_MODELS if m != preferred_model]
                  if preferred_model else _OR_MODELS)
        content = _or_call(messages, temperature=0.85, max_tokens=600, models=models)
    except Exception as e:
        raise HTTPException(502, f"OpenRouter error: {e}")

    m = _re.search(r'\{.*\}', content, _re.DOTALL)
    if not m:
        raise HTTPException(502, "risposta LLM non parsabile")
    try:
        result = json.loads(m.group())
    except Exception:
        raise HTTPException(502, "JSON non valido dalla risposta LLM")

    reply = result.get("reply") or "Ecco la tua playlist."
    playlist_name = result.get("playlist_name") or "Playlist DigChat"
    indices = result.get("track_indices") or []

    selected = []
    for idx in indices:
        if isinstance(idx, int) and 0 <= idx < len(tracks):
            t = dict(tracks[idx])
            t["_yt_cover"] = f"https://i.ytimg.com/vi/{t['video_id']}/hqdefault.jpg"
            selected.append(t)

    if not selected:
        raise HTTPException(502, "nessun brano selezionato dal modello")

    pl = {
        "id": "digchat_" + uuid.uuid4().hex[:10],
        "name": playlist_name,
        "profile": "digchat",
        "savedAt": _dt.datetime.now().isoformat(),
        "tracks": selected,
    }
    lib = _lib_read()
    lib = [p for p in lib if p.get("id") != pl["id"]]
    lib.insert(0, pl)
    _lib_write(lib)

    return {"reply": reply, "playlist_name": playlist_name, "playlist_id": pl["id"], "tracks": selected}


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


@app.post("/api/embed")
async def embed_audio(profile: str = Form(...), audio: UploadFile = File(...)):
    """Stateless CLAP scorer for the diggaplayer Android app: it resolves a
    candidate's audio on-device (NewPipeYoutube — a legitimate mobile/home
    IP) and uploads a snippet here rather than having GitHub Actions try to
    fetch YouTube itself, which YouTube blocks outright for that class of IP
    even with valid session cookies (SignInConfirmNotBotException, tested
    live — a session-provenance/anti-hijack check, not a cookie-validity
    one, so nothing on the fetching side can route around it). This endpoint
    never touches YouTube at all — just audio bytes in, a CLAP similarity
    score out.

    `audio` is whatever the phone managed to download (often a byte-range
    slice of a DASH/WebM stream, not a complete container) — ffmpeg is
    tolerant of that, same as yt_hunter.download_snippet already relies on
    for its own snippets.
    """
    if profile not in profiles.PROFILES:
        raise HTTPException(404, f"profilo sconosciuto: {profile}")
    prof_emb = profiles.get_embedding(profile)
    if prof_emb is None:
        raise HTTPException(400, f"profilo '{profile}' non ha un embedding pronto")

    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "audio vuoto")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "audio troppo grande (max 8MB)")

    import subprocess
    import tempfile

    import numpy as np
    import clap_model

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp_in:
        tmp_in.write(raw)
        tmp_in_path = tmp_in.name
    tmp_out_path = tmp_in_path + ".mp3"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_in_path, "-t", "30", "-vn",
             "-acodec", "libmp3lame", "-b:a", "128k", tmp_out_path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0 or not os.path.exists(tmp_out_path) or os.path.getsize(tmp_out_path) == 0:
            raise HTTPException(422, f"audio non decodificabile: {proc.stderr[-300:]}")

        clap = clap_model.embed_audio(tmp_out_path)
        sim = float(np.clip(np.dot(np.asarray(prof_emb), np.asarray(clap)), 0.0, 1.0))
        return {"profile": profile, "sim": round(sim, 4), "vibe": int(round(sim * 100))}
    finally:
        for p in (tmp_in_path, tmp_out_path):
            try:
                os.remove(p)
            except OSError:
                pass


@app.post("/api/embed_raw")
async def embed_raw_audio(audio: UploadFile = File(...), offset_sec: float = Form(0.0)):
    """Stateless raw CLAP embedding extractor for COLOSSO and DIGMORE v2.
    Accepts an audio snippet (up to 8MB), returns the 512-dim L2-normalized
    embedding vector without computing similarity against a fixed profile.

    `offset_sec` picks where the 30-second analysis window starts inside the
    upload. It exists because the caller CANNOT skip the intro itself: a
    byte-range chunk taken from the middle of a WebM/Opus stream carries no
    container header and ffmpeg rejects it outright ("Invalid data found when
    processing input" — measured against real googlevideo URLs on 04/09/2026,
    which is why every DIGMORE embedding silently failed until then). So the
    phone sends a container-valid chunk starting at byte 0 and says how far in
    the interesting part is; seeking is cheap here and impossible there.
    Defaults to 0, so existing callers (COLOSSO) are unaffected.
    """
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "audio vuoto")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "audio troppo grande (max 8MB)")

    import subprocess
    import tempfile
    import numpy as np
    import clap_model

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp_in:
        tmp_in.write(raw)
        tmp_in_path = tmp_in.name
    tmp_out_path = tmp_in_path + ".mp3"
    try:
        def _decode(seek: float) -> bool:
            """ffmpeg -ss BEFORE -i seeks by keyframe and is fast; a chunk
            shorter than `seek` yields an empty file rather than an error,
            which is why the caller checks the size and not the return code."""
            args = ["ffmpeg", "-y"]
            if seek > 0:
                args += ["-ss", str(seek)]
            args += ["-i", tmp_in_path, "-t", "30", "-vn",
                     "-acodec", "libmp3lame", "-b:a", "128k", tmp_out_path]
            p = subprocess.run(args, capture_output=True, text=True, timeout=30)
            return (p.returncode == 0 and os.path.exists(tmp_out_path)
                    and os.path.getsize(tmp_out_path) > 4096)

        seek = max(0.0, float(offset_sec or 0.0))
        # Falling back to the start matters: a short track (or a small chunk)
        # has nothing at the requested offset, and analysing its opening is
        # far better than refusing to analyse it at all.
        if not _decode(seek) and not (seek > 0 and _decode(0.0)):
            raise HTTPException(422, "audio non decodificabile")

        emb = clap_model.embed_audio(tmp_out_path)
        emb_arr = np.asarray(emb, dtype=np.float32)
        norm = float(np.linalg.norm(emb_arr))
        if norm > 1e-9:
            emb_arr = emb_arr / norm
        return {"embedding": emb_arr.tolist(), "dim": len(emb_arr)}
    finally:
        for p in (tmp_in_path, tmp_out_path):
            try:
                os.remove(p)
            except OSError:
                pass


@app.post("/api/embed_text")
async def embed_text(request: Request):
    """Text-side CLAP embedding for FIDES DAYS (diggaplayer): CLAP is a joint
    text/audio space by design, so a mood/genre description can be embedded
    directly here instead of the caller having to invent proxy reference
    tracks, fetch their audio and embed THAT. Several short English
    descriptive phrases (not full sentences) are mean-pooled and
    re-normalized into one vector — the same multi-prompt-averaging trick
    `clap_model.guess_genre` already uses internally for its own genre
    prompt sets, just returned to the caller instead of used for a lookup.
    """
    body = await request.json()
    prompts = [p.strip() for p in (body.get("prompts") or []) if isinstance(p, str) and p.strip()]
    if not prompts:
        raise HTTPException(400, "prompts vuoti")

    import numpy as np
    import clap_model

    embs = clap_model.embed_texts(prompts)
    mean = embs.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm > 1e-9:
        mean = mean / norm
    return {"embedding": mean.tolist(), "dim": len(mean)}


# ── Static UI (local dev / single-host deploy) ───────────────────────────────
_WEB = (Path(__file__).resolve().parents[1] / "web")
if _WEB.is_dir():
    @app.get("/", include_in_schema=False)
    def root():
        idx = _WEB / "index.html"
        return FileResponse(str(idx)) if idx.exists() else JSONResponse({"ok": True})

    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
