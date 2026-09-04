"""Audio descriptors used only to sequence exported playlists."""
import numpy as np
import librosa

import clap_model
import features
import yt_hunter
from cache import seq_get, seq_put

INSTR_PROMPTS = [
    "a male singer singing lead vocals", "a female singer singing lead vocals",
    "a vocal group singing harmonies", "an instrumental track with no singing",
    "acoustic piano playing", "a hammond organ playing",
    "an acoustic guitar playing", "an electric guitar playing",
    "a horn section playing brass", "a saxophone solo", "a flute playing",
    "a string orchestra playing", "a vibraphone playing",
    "an analog synthesizer playing", "an upright acoustic bass playing",
    "a drum kit playing a groove", "latin percussion playing",
    "a harpsichord or harp playing",
]
INSTR_LABELS = [
    "voce m.", "voce f.", "cori", "strumentale", "piano", "organo",
    "chit. ac.", "chit. el.", "fiati", "sax", "flauto", "archi",
    "vibrafono", "synth", "contrabbasso", "batteria", "percussioni",
    "clavicembalo",
]
_text_embeddings = None


def _norm(v):
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    return v / (np.linalg.norm(v) + 1e-9)


def instrumentation(clap_vec) -> dict:
    global _text_embeddings
    if _text_embeddings is None:
        raw = np.asarray(clap_model.embed_texts(INSTR_PROMPTS), dtype=np.float32)
        _text_embeddings = raw / (np.linalg.norm(raw, axis=1, keepdims=True) + 1e-9)
    audio = _norm(clap_vec)
    logits = (_text_embeddings @ audio) / 0.03
    weights = np.exp(logits - np.max(logits))
    weights /= weights.sum() + 1e-9
    top = np.argsort(weights)[-2:][::-1]
    return {"instr": weights.astype(np.float32),
            "instr_top": [INSTR_LABELS[int(i)] for i in top]}


def features_from_path(path: str, clap_vec=None) -> dict:
    y, sr = librosa.load(path, sr=features.ANALYSIS_SR, mono=True, duration=180,
                         res_type="kaiser_fast")
    if len(y) / sr < 0.5:
        raise ValueError("audio troppo corto")
    y = y / (np.max(np.abs(y)) + 1e-9)
    try:
        harmonic = librosa.effects.harmonic(y, margin=3.0)
    except Exception:
        harmonic = y
    base = features._window_vectors(harmonic, sr)
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=features.HOP)
    bpm_raw = librosa.beat.beat_track(onset_envelope=onset, sr=sr,
                                      hop_length=features.HOP)[0]
    bpm = float(np.asarray(bpm_raw).reshape(-1)[0]) if np.size(bpm_raw) else 0.0
    rms_frames = librosa.feature.rms(y=y, hop_length=features.HOP).reshape(-1)
    rms_mean = float(np.mean(rms_frames))
    dbfs = float(librosa.amplitude_to_db(np.array([max(rms_mean, 1e-9)]), ref=1.0)[0])
    clap = _norm(clap_vec) if clap_vec is not None else _norm(clap_model.embed_audio_array(y, sr))
    instr = instrumentation(clap)
    return {
        "ok": True, "key": base["key"], "mode": base["mode"],
        "camelot": base["camelot"], "key_conf": float(base["key_conf"]),
        "bpm": round(bpm, 2), "energy": float(np.clip((dbfs + 40) / 40, 0, 1)),
        "density": float(np.tanh(np.mean(onset) / 2.0)),
        "brightness": float(np.clip(base["centroid"] / 8000, 0, 1)),
        "dynamics": float(np.std(rms_frames) / (rms_mean + 1e-9)),
        "band_energy": base["band_energy"], "mfcc": _norm(base["mfcc_mean"]),
        "chroma": _norm(base["chroma_mean"]), "tonnetz": _norm(base["tonnetz_mean"]),
        "clap": clap, **instr,
    }


def features_for(video_id: str, *, allow_download: bool = True,
                 reanalyze: bool = False) -> dict | None:
    if not reanalyze:
        cached = seq_get(video_id)
        if cached is not None:
            return cached
    if not allow_download:
        return None
    try:
        path = yt_hunter.download_snippet(video_id, start=45, dur=45)
        feat = features_from_path(path)
        seq_put(video_id, feat)
        return feat
    except Exception as exc:
        print(f"[sequence] analisi fallita {video_id}: {exc}", flush=True)
        return None
