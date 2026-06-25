"""Lazy singleton wrapper around laion-clap (music checkpoint).

Provides audio + text embeddings and zero-shot genre/macro classification.
"""
import threading

import numpy as np

_MODEL = None
# The model is a single shared torch object. We run inference from several
# worker threads (parallel candidate analysis), so every forward pass is
# serialized through this lock. Downloads + librosa run *outside* the lock and
# still parallelize — CLAP is the only piece that must be one-at-a-time.
_INFER_LOCK = threading.Lock()


def _get_model():
    global _MODEL
    if _MODEL is None:
        import laion_clap
        print("[CLAP] Loading model (first run downloads the checkpoint)...")
        m = laion_clap.CLAP_Module(enable_fusion=False)
        m.load_ckpt()
        _MODEL = m
        print("[CLAP] Ready.")
    return _MODEL


def embed_audio(path: str) -> np.ndarray:
    """512-dim L2-normalized embedding for an audio file."""
    m = _get_model()
    with _INFER_LOCK:
        emb = m.get_audio_embedding_from_filelist(x=[path], use_tensor=False)
    v = np.asarray(emb[0], dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def embed_audio_array(y: np.ndarray, sr: int) -> np.ndarray:
    """512-dim L2-normalized embedding for an in-memory mono signal.

    laion-clap only reads from disk, so we stage the window in a temp wav.
    """
    import os
    import tempfile
    import soundfile as sf

    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        sf.write(tmp, y, sr)
        return embed_audio(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def embed_texts(prompts: list[str]) -> np.ndarray:
    """(N, 512) L2-normalized text embeddings."""
    m = _get_model()
    with _INFER_LOCK:
        emb = m.get_text_embedding(prompts, use_tensor=False)
    v = np.asarray(emb, dtype=np.float32)
    norms = np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
    return v / norms


# Multiple prompts per macro category — averaged for a richer, more stable
# embedding that CLAP can reliably match against audio.
GENRE_PROMPTS: dict[str, list[str]] = {
    "soul": [
        "vintage soul music with warm Rhodes piano organ and electric bass groove",
        "1970s rhythm and blues soul with lush string arrangements and gospel vocals",
        "funky soul sample with wah guitar brass section and tight drum groove",
        "Philadelphia soul or Motown style smooth soulful instrumental",
        "deep soul ballad with organ and slow melancholic chord progression",
    ],
    "jazz": [
        "soul jazz instrumental recording with piano upright bass and brushed drums",
        "1970s jazz fusion sample with electric piano saxophone and modal harmonies",
        "hard bop jazz with trumpet muted and walking bass line",
        "jazz funk groove with organ comping and crisp snare rim shots",
        "contemporary jazz with sophisticated chord voicings and vibraphone",
    ],
    "brazilian": [
        "bossa nova guitar with syncopated samba rhythm and warm bass",
        "MPB Brazilian music with rich harmonies flute and acoustic guitar",
        "Brazilian samba soul percussion with jazz influenced arrangement",
        "Jobim style bossa nova with nylon string guitar and light percussion",
        "Tropicália or MPB record with orchestral arrangement and Brazilian rhythm",
    ],
    "ost": [
        "cinematic film soundtrack with dramatic orchestral strings and piano",
        "1970s blaxploitation movie score with funky bass wah guitar and brass",
        "Italian library music with Moog synthesizer strings and suspense atmosphere",
        "French film score with melancholic piano strings and jazz influences",
        "movie soundtrack with lush orchestral arrangement and dramatic tension",
    ],
}


def guess_genre(sample_emb: np.ndarray) -> tuple[str, dict]:
    """Zero-shot macro genre classification using averaged multi-prompt embeddings."""
    scores = {}
    for key, prompts in GENRE_PROMPTS.items():
        txt_embs = embed_texts(prompts)          # (N, 512)
        mean_emb = txt_embs.mean(axis=0)
        mean_emb = mean_emb / (np.linalg.norm(mean_emb) + 1e-9)
        scores[key] = round(float(mean_emb @ sample_emb), 3)
    best = max(scores, key=scores.get)
    return best, scores
