"""Feature extraction for the drumless sample and for YouTube candidates.

The reference is a clean drumless loop; YouTube candidates are full songs WITH
drums, downloaded from an arbitrary slice of the track. Two ideas make the
comparison fair without running Demucs on every candidate:

  1. DRUM-ROBUST features (HPSS) — harmonic/timbral/spectral features are
     computed on librosa's *harmonic* component, so the candidate's drums stop
     polluting MFCC / chroma / contrast / tonnetz. Applied to BOTH sides so they
     live in the same processing domain. CLAP stays on the raw audio (it's
     trained on full mixes and is robust to drums — running it on HPSS output
     would be out of distribution).

  2. MULTI-SEGMENT (candidates only) — a full song is split into several windows
     and every vector feature becomes a 2-D array (n_segments × dim). The scorer
     max-pools over segments, so the *sampled* section is found even if it sits
     deep in the track instead of in a fixed 30-90s window.

Features extracted per file:
  - CLAP embedding (512-dim)          overall vibe / style / instrumentation
  - MFCCs (60-dim)                    timbre / instrument texture
  - Chroma mean (12-dim)              harmonic color / chord voicing
  - Tonnetz (6-dim)                   tonal centroid / harmonic relations
  - Spectral contrast (7-dim)         peak-vs-valley per band
  - Spectral vector (6-dim)           brightness / energy profile
  - Tempogram slice (30-dim)          rhythmic periodicity pattern
  - key, mode, camelot, key_conf      detected tonality
  - bpm, onset_density                tempo / rhythmic feel
"""
import sys
from pathlib import Path

import numpy as np
import librosa

# ── Reuse loopforge (sibling project under sre/loopforge) ──────────────────
_LOOPFORGE = Path(__file__).resolve().parents[2] / "loopforge"
if _LOOPFORGE.is_dir() and str(_LOOPFORGE) not in sys.path:
    sys.path.insert(0, str(_LOOPFORGE))

from analysis import _detect_key, _detect_bpm_bars, N_FFT, HOP  # noqa: E402
from matcher import _key_compat  # noqa: E402

import clap_model    # noqa: E402
import chop_filter   # noqa: E402

ANALYSIS_SR = 22050
# loopforge's _load caps at 10s (it's built for short loops); candidates are
# full songs, so we load directly and only cap to keep analysis bounded.
_MAX_LOAD_SEC = 180.0


def _load_audio(path: str):
    """Load mono audio at ANALYSIS_SR, up to _MAX_LOAD_SEC (no 10s loop cap)."""
    y, _ = librosa.load(path, sr=ANALYSIS_SR, mono=True,
                        duration=_MAX_LOAD_SEC, res_type="kaiser_fast")
    return y, ANALYSIS_SR

# Multi-segment windowing for candidates
_SEG_LEN = 35.0      # seconds per analysis window
_MAX_SEGS = 3        # up to N windows spread across the snippet
# HPSS aggressiveness: margin > 1 separates harmonic/percussive more strongly
_HPSS_MARGIN = 3.0


def key_compat(src: str, tgt: str) -> float:
    return _key_compat(src, tgt)


def _norm(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


# ── Per-window feature computation ───────────────────────────────────────────

def _window_vectors(y_h: np.ndarray, sr: int) -> dict:
    """Vector + scalar features for one harmonic-component window."""
    S = np.abs(librosa.stft(y_h, n_fft=N_FFT, hop_length=HOP))
    S_pow = S ** 2

    chroma = librosa.feature.chroma_stft(S=S_pow, sr=sr, n_fft=N_FFT, hop_length=HOP)
    chroma_mean = _norm(np.mean(chroma, axis=1).astype(np.float32))

    mfcc = librosa.feature.mfcc(y=y_h, sr=sr, n_mfcc=20, n_fft=N_FFT, hop_length=HOP)
    mfcc_delta = librosa.feature.delta(mfcc)
    mfcc_mean = _norm(np.concatenate([
        np.mean(mfcc, axis=1),
        np.mean(mfcc_delta, axis=1),
        np.std(mfcc, axis=1),
    ]).astype(np.float32))

    try:
        contrast = librosa.feature.spectral_contrast(S=S, sr=sr, n_fft=N_FFT, hop_length=HOP)
        contrast_mean = _norm(np.mean(contrast, axis=1).astype(np.float32))
    except Exception:
        contrast_mean = np.zeros(7, dtype=np.float32)

    try:
        tonnetz = librosa.feature.tonnetz(y=y_h, sr=sr)
        tonnetz_mean = _norm(np.mean(tonnetz, axis=1).astype(np.float32))
    except Exception:
        tonnetz_mean = np.zeros(6, dtype=np.float32)

    centroid  = float(np.mean(librosa.feature.spectral_centroid(S=S, sr=sr, n_fft=N_FFT)))
    rolloff   = float(np.mean(librosa.feature.spectral_rolloff(S=S, sr=sr, n_fft=N_FFT)))
    bandwidth = float(np.mean(librosa.feature.spectral_bandwidth(S=S, sr=sr, n_fft=N_FFT)))
    flatness  = float(np.mean(librosa.feature.spectral_flatness(S=S)))
    rms       = float(np.mean(librosa.feature.rms(y=y_h, hop_length=HOP)))
    zcr       = float(np.mean(librosa.feature.zero_crossing_rate(y_h, hop_length=HOP)))

    spec_vec = _norm(np.array(
        [centroid / 22050, rolloff / 22050, bandwidth / 22050,
         flatness, rms * 10, zcr * 10],
        dtype=np.float32
    ))

    freqs = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    total_S = float(np.sum(S)) + 1e-12
    band_energy = {
        "sub":  float(np.sum(S[freqs < 100])) / total_S,
        "low":  float(np.sum(S[(freqs >= 100) & (freqs < 300)])) / total_S,
        "mid":  float(np.sum(S[(freqs >= 300) & (freqs < 3000)])) / total_S,
        "high": float(np.sum(S[freqs >= 3000])) / total_S,
    }

    key, mode, camelot, kc = _detect_key(chroma)

    return {
        "chroma_mean": chroma_mean, "mfcc_mean": mfcc_mean,
        "contrast_mean": contrast_mean, "tonnetz_mean": tonnetz_mean,
        "spec_vec": spec_vec,
        "key": key, "mode": mode, "camelot": camelot, "key_conf": kc,
        "centroid": round(centroid, 1), "rolloff": round(rolloff, 1),
        "rms": round(rms, 5), "zcr": round(zcr, 5),
        "band_energy": band_energy,
    }


def _segment_bounds(n: int, sr: int) -> list[tuple[int, int]]:
    """Sample-index windows covering the signal (1 window for short clips)."""
    total = n / sr
    seg_n = int(_SEG_LEN * sr)
    if total <= _SEG_LEN * 1.25:
        return [(0, n)]
    starts = np.linspace(0, n - seg_n, _MAX_SEGS).astype(int)
    return [(s, s + seg_n) for s in starts]


class NoSampleableWindows(ValueError):
    """Raised when chop_guided=True and no window passes the sampleability gate."""


def extract(path: str, with_clap: bool = True,
            drum_robust: bool = True, segment: bool = False,
            chop_guided: bool = False,
            chop_strength_factor: float = 1.5) -> dict:
    """Return all computable features for an audio file.

    drum_robust          : compute harmonic/timbral features on the HPSS harmonic
                           component so drums in the candidate don't pollute the match.
    segment              : split a long candidate into fixed windows; every vector
                           feature becomes a 2-D array (n_segments × dim) for
                           max-pooled scoring. Ignored when chop_guided=True.
    chop_guided          : use onset-detection windows (sampleChop engine) instead
                           of fixed intervals. Windows that don't pass the
                           sampleability gate are excluded. Raises NoSampleableWindows
                           if no window passes — caller should skip this candidate.
    chop_strength_factor : onset strength gate; a window must be ≥ this many times
                           the global mean onset strength to be considered sampleable.
                           1.5 = 50% above average (default). Increase for stricter
                           filtering.
    """
    y, sr = _load_audio(path)
    if len(y) / sr < 0.5:
        raise ValueError("audio too short")

    # Peak-normalize so a quiet loop and a loud full track are comparable.
    y = y / (np.max(np.abs(y)) + 1e-9)

    # ── Window selection ──────────────────────────────────────────────────────
    chop_windows_meta: list[dict] = []
    if chop_guided:
        chop_windows_meta = chop_filter.filter_sampleable(
            y, sr,
            strength_factor=chop_strength_factor,
        )
        if not chop_windows_meta:
            raise NoSampleableWindows(
                "no sampleable windows found (all onset strengths below gate)"
            )
        bounds = [
            (int(w["start_sec"] * sr), min(int(w["end_sec"] * sr), len(y)))
            for w in chop_windows_meta
        ]
    else:
        bounds = _segment_bounds(len(y), sr) if segment else [(0, len(y))]

    per_seg = []
    clap_rows = []
    for a, b in bounds:
        win = y[a:b]
        # HPSS per window (not on the gaps between windows) → drum-robust + cheaper.
        if drum_robust:
            try:
                y_h = librosa.effects.harmonic(win, margin=_HPSS_MARGIN)
            except Exception:
                y_h = win
        else:
            y_h = win
        per_seg.append(_window_vectors(y_h, sr))
        if with_clap:
            # CLAP on the raw (drummed) window — it's robust and in-domain.
            clap_rows.append(clap_model.embed_audio_array(win, sr))

    # Stack vector features across segments (2-D for candidates, 1-D for ref).
    vec_keys = ["chroma_mean", "mfcc_mean", "contrast_mean", "tonnetz_mean", "spec_vec"]
    out: dict = {}
    if len(per_seg) == 1:
        for k in vec_keys:
            out[k] = per_seg[0][k]
    else:
        for k in vec_keys:
            out[k] = np.vstack([s[k] for s in per_seg]).astype(np.float32)

    # Scalar tonality: take the window with the highest key confidence.
    best = max(per_seg, key=lambda s: s.get("key_conf", 0))
    out.update({
        "key": best["key"], "mode": best["mode"],
        "camelot": best["camelot"], "key_conf": best["key_conf"],
        "centroid": best["centroid"], "rolloff": best["rolloff"],
        "rms": best["rms"], "zcr": best["zcr"],
        "band_energy": best["band_energy"],
    })

    # ── Rhythm (on the ORIGINAL signal — harmonic-only kills the groove) ─────
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    out["onset_density"] = round(float(np.mean(onset_env)), 4)
    try:
        tempogram = librosa.feature.tempogram(
            onset_envelope=onset_env, sr=sr, hop_length=HOP)
        out["tempogram_mean"] = _norm(np.mean(tempogram, axis=1)[:30].astype(np.float32))
    except Exception:
        out["tempogram_mean"] = np.zeros(30, dtype=np.float32)
    out["bpm"], _, _ = _detect_bpm_bars(len(y) / sr)

    # ── CLAP ─────────────────────────────────────────────────────────────────
    if with_clap and clap_rows:
        out["clap"] = (clap_rows[0] if len(clap_rows) == 1
                       else np.vstack(clap_rows).astype(np.float32))
    else:
        out["clap"] = None

    # ── Chop metadata (only when chop_guided=True) ────────────────────────────
    # Stored so the caller (main.py) can map the best-scoring CLAP row back
    # to a concrete timestamp in the snippet → YouTube &t= deep-link.
    out["chop_windows_meta"] = chop_windows_meta  # [] when not in chop mode

    return out
