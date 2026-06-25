---
title: DIGMORE Engine
emoji: 🎶
colorFrom: red
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# DIGMORE

Generatore di playlist da crate-digging: scegli un **profilo musicale**
(Chill Soul / Relax Jazz / Relax OST / Brazil), l'app pesca su **Discogs** dischi
**poco conosciuti ma ben votati** del **1969–1983**, scrapa le tracklist, e tiene
solo i brani la cui **vibe** (similarità CLAP col profilo) supera la soglia
(default **55%**), fino a **30 brani**. Esclude ciò che hai **già ascoltato**
(anche fuori dall'app) e cambia materiale a ogni generazione. Riproduzione via
**YouTube nascosto (solo audio)** — si vede solo la **copertina** del disco.

Riusa l'engine CLAP di **SampleHunter** (vendorizzato in `engine/engine_libs/`).

## Struttura

```
digmore/
  Dockerfile            ← build dell'engine per Hugging Face Spaces
  engine/               ← FastAPI + CLAP + yt-dlp (gira su HF Spaces)
    app.py              ← API (vedi sotto)
    profiles.py         ← CSV reference → embedding CLAP per profilo
    digger.py           ← orchestratore playlist (round Discogs → gate vibe → 30)
    discogs_ext.py      ← Discogs + filtro voto + mapping macrogenere + anni
    listened_store.py   ← store persistente "già ascoltati"
    engine_libs/        ← moduli SampleHunter + loopforge vendorizzati
    profiles_csv/       ← <<< METTI QUI I CSV: chill_soul.csv, relax_jazz.csv, relax_ost.csv, brazil.csv
    requirements.txt
  web/                  ← UI statica (gira su Vercel)  index.html + app.js
```

## CSV dei profili

Metti i file in `engine/profiles_csv/` con i nomi: `chill_soul.csv`, `relax_jazz.csv`,
`relax_ost.csv`, `brazil.csv`. Formato auto-rilevato:
- con intestazione → colonne riconosciute per nome (`artist`/`title`/`track`/`song`/`name`)
- 2 colonne senza header → `(artista, titolo)`
- 1 colonna → solo titolo

## API (prefisso `/api`)

| Metodo | Rotta | Descrizione |
|---|---|---|
| GET  | `/api/profiles` | profili + stato (ready / not_built / no_csv) |
| POST | `/api/profiles/{id}/build` | costruisce l'embedding dal CSV (async) |
| GET  | `/api/profiles/build-status/{job}` | progresso build |
| POST | `/api/generate` | avvia generazione (form: `profile`, `vibe_gate`, …) |
| GET  | `/api/generate/{job}` | progresso + risultati |
| POST | `/api/generate/{job}/stop` | ferma |
| POST | `/api/listened/import` | aggiunge brani ascoltati (form `text` o `file`) |
| POST | `/api/listened/mark` | auto-mark al play |
| GET  | `/api/cover?u=…` | proxy copertina Discogs/YouTube |

## Run locale

```bash
# usa l'ambiente Python che ha le dipendenze (es. il venv di samplehunter)
python -m uvicorn app:app --app-dir digmore/engine --port 8099
# poi apri http://localhost:8099  (l'engine serve anche la UI da ../web)
```
Su Windows: doppio clic su `run.bat`.

## Deploy

**Engine → Hugging Face Spaces (Docker):**
1. Crea uno Space *Docker*. Aggiungi (consigliato) un **persistent disk** montato su `/data`
   così profili / ascoltati / storico sopravvivono ai riavvii.
2. Pusha questo repo sul remote dello Space. HF builda dal `Dockerfile` e avvia sulla 7860.
3. Costruisci i profili una volta: `POST /api/profiles/<id>/build` per ciascuno.

**UI → Vercel:**
1. Importa il repo privato GitHub su Vercel, **Root Directory = `web/`** (progetto statico).
2. Apri la UI → menu **Engine** → incolla l'URL dello Space (es. `https://<tuo>.hf.space`).
   (oppure imposta `window.DIGMORE_ENGINE` in un piccolo script.)

> ⚠️ **yt-dlp da IP datacenter**: YouTube può bloccare i download da cloud. Se succede sullo
> Space, monta un file `cookies.txt` e/o un proxy residenziale e passali a yt-dlp.

> ⚠️ **numpy<2**: vincolo di laion-clap. Non aggiornare numpy o CLAP smette di caricarsi.
