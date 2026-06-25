"""
Fast loop analysis — target <0.3s per WAV file.
Key optimizations:
  - No beat_track (O(n²) DP) — use bar-snap from duration (more accurate for loops)
  - Fast resampling: kaiser_fast instead of kaiser_best
  - Single STFT reused across all feature functions
  - soundfile for lossless formats (no audioread subprocess)
"""
import numpy as np
import librosa
import soundfile as sf
import json
from pathlib import Path

# Krumhansl-Schmuckler profiles
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

NOTE_NAMES  = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
CAMELOT_MAJ = ["8B","9B","10B","11B","12B","1B","2B","3B","4B","5B","6B","7B"]
CAMELOT_MIN = ["5A","6A","7A","8A","9A","10A","11A","12A","1A","2A","3A","4A"]

ANALYSIS_SR  = 22050
MAX_DURATION = 10.0
N_FFT        = 1024
HOP          = 256
LOSSLESS_EXT = {".wav", ".flac", ".aiff", ".aif", ".w64"}

# Pre-computed BPM grid for bar-snap detection (60–200 BPM, 0.5 resolution)
_BPM_GRID = np.arange(60.0, 200.5, 0.5)


# ─── Fast audio loader ────────────────────────────────────────────

def _load(filepath: str):
    p = Path(filepath)
    if p.suffix.lower() in LOSSLESS_EXT:
        try:
            info    = sf.info(filepath)
            frames  = int(MAX_DURATION * info.samplerate)
            y, fsr  = sf.read(filepath, frames=frames, dtype="float32", always_2d=False)
            if y.ndim > 1:
                y = np.mean(y, axis=1)
            if fsr != ANALYSIS_SR:
                y = librosa.resample(y, orig_sr=fsr, target_sr=ANALYSIS_SR,
                                     res_type="kaiser_fast")   # << fast
            return y, ANALYSIS_SR
        except Exception:
            pass
    # fallback: librosa handles MP3/OGG via audioread
    return librosa.load(filepath, sr=ANALYSIS_SR, mono=True,
                        duration=MAX_DURATION, res_type="kaiser_fast")


# ─── BPM + bars (no beat_track) ──────────────────────────────────

def _detect_bpm_bars(duration: float):
    """
    For loops, duration ≈ N_bars × 4 beats × (60/BPM).
    Find the (N_bars, BPM) pair whose expected duration is closest to actual.
    This is O(n_bars × n_bpm) but n is tiny — runs in microseconds.
    More accurate than beat_track for loop material.
    """
    best_bpm, best_bars, best_err = 120.0, 4, float("inf")

    for nb in [1, 2, 4, 8, 16]:
        # expected_dur = nb * 4 * 60 / bpm  →  bpm = nb * 4 * 60 / duration
        bpm_exact = nb * 4 * 60.0 / duration
        if not (55 <= bpm_exact <= 210):
            continue
        # Snap to nearest grid point
        idx      = int(np.argmin(np.abs(_BPM_GRID - bpm_exact)))
        bpm_snap = float(_BPM_GRID[idx])
        exp_dur  = nb * 4 * 60.0 / bpm_snap
        err      = abs(duration - exp_dur)

        if err < best_err:
            best_err, best_bpm, best_bars = err, bpm_snap, nb

    # If nothing fit well, try half/double time
    if best_err > 0.3:
        for nb in [1, 2, 4, 8, 16]:
            for factor in [0.5, 2.0]:
                bpm_try = (nb * 4 * 60.0 / duration) * factor
                if not (60 <= bpm_try <= 200):
                    continue
                idx     = int(np.argmin(np.abs(_BPM_GRID - bpm_try)))
                bpm_try = float(_BPM_GRID[idx])
                exp_dur = nb * 4 * 60.0 / (bpm_try / factor)
                err     = abs(duration - exp_dur)
                if err < best_err:
                    best_err, best_bpm, best_bars = err, bpm_try / factor, nb

    while best_bpm < 65:  best_bpm *= 2
    while best_bpm > 190: best_bpm /= 2
    return round(best_bpm, 1), best_bars, 4


# ─── Key detection ────────────────────────────────────────────────

def _detect_key(chroma: np.ndarray):
    mc = np.mean(chroma, axis=1)
    best, ki, mode = -np.inf, 0, "major"
    for i in range(12):
        m  = float(np.corrcoef(np.roll(_KS_MAJOR, i), mc)[0, 1])
        mn = float(np.corrcoef(np.roll(_KS_MINOR, i), mc)[0, 1])
        if m  > best: best, ki, mode = m,  i, "major"
        if mn > best: best, ki, mode = mn, i, "minor"
    camelot = CAMELOT_MAJ[ki] if mode == "major" else CAMELOT_MIN[ki]
    conf    = float(np.clip((best + 1) / 2, 0, 1))
    return NOTE_NAMES[ki], mode, camelot, round(conf, 3)


# ─── Role classification ─────────────────────────────────────────

def _classify_role(y, sr, S, freqs, onset_frames):
    dur          = len(y) / sr
    onset_density = len(onset_frames) / max(dur, 0.1)
    centroid      = float(np.mean(librosa.feature.spectral_centroid(S=S, sr=sr, n_fft=N_FFT)))
    flatness      = float(np.mean(librosa.feature.spectral_flatness(S=S)))
    zcr           = float(np.mean(librosa.feature.zero_crossing_rate(y)))

    total_S = float(np.sum(S)) + 1e-12
    sub_e   = float(np.sum(S[freqs < 100]))  / total_S
    low_e   = float(np.sum(S[(freqs >= 100) & (freqs < 300)])) / total_S
    mid_e   = float(np.sum(S[(freqs >= 300) & (freqs < 3000)])) / total_S
    hi_e    = float(np.sum(S[freqs >= 3000]))  / total_S

    band = {"sub": round(sub_e,4), "low": round(low_e,4),
            "mid": round(mid_e,4), "high": round(hi_e,4)}

    percussive  = onset_density > 5 and zcr > 0.08
    bass_like   = centroid < 900 and (sub_e + low_e) > 0.25 and not percussive
    tonal       = flatness < 0.18 and centroid > 500

    if percussive and onset_density > 6:       role = "drums"
    elif bass_like:                            role = "bass"
    elif tonal:                                role = "harmonic_melodic"
    elif flatness > 0.35:                      role = "texture_fx"
    else:                                      role = "full"

    drum_score = 0.0
    if role == "drums" or onset_density > 4:
        ot = librosa.frames_to_time(onset_frames, sr=sr)
        if len(ot) > 3:
            gaps       = np.diff(ot)
            regularity = 1.0 / (np.std(gaps) / (np.mean(gaps) + 1e-6) + 0.1)
        else:
            regularity = 0.5
        drum_score = min(1.0, zcr * 10) * min(1.0, regularity / 5.0)

    return role, band, round(drum_score, 3)


# ─── Peaks ───────────────────────────────────────────────────────

def _peaks(y, n=100):
    y     = np.abs(y)
    chunk = max(1, len(y) // n)
    vals  = [float(np.max(y[i:i+chunk])) for i in range(0, len(y), chunk)]
    vals  = vals[:n]
    mx    = max(vals) if vals else 1.0
    vals  = [v / (mx + 1e-10) for v in vals]
    while len(vals) < n:
        vals.append(0.0)
    return vals


# ─── Public entry ────────────────────────────────────────────────

def analyze_loop(filepath: str) -> dict:
    try:
        y, sr = _load(filepath)
    except Exception as e:
        return {"error": str(e)}

    duration = len(y) / sr
    if duration < 0.3:
        return {"error": "too short"}

    S      = np.abs(librosa.stft(y, n_fft=N_FFT, hop_length=HOP))
    freqs  = librosa.fft_frequencies(sr=sr, n_fft=N_FFT)
    chroma = librosa.feature.chroma_stft(S=S**2, sr=sr, n_fft=N_FFT, hop_length=HOP)
    onsets = librosa.onset.onset_detect(y=y, sr=sr, hop_length=HOP, units="frames")

    bpm, bars, bpb  = _detect_bpm_bars(duration)
    key, mode, cal, kc = _detect_key(chroma)
    role, band, dsc = _classify_role(y, sr, S, freqs, onsets)
    pk              = _peaks(y)

    return {
        "duration":      round(duration, 3),
        "bpm":           bpm,
        "bars":          bars,
        "beats_per_bar": bpb,
        "key":           key,
        "mode":          mode,
        "camelot":       cal,
        "key_conf":      kc,
        "role":          role,
        "band_energy":   json.dumps(band),
        "drum_score":    dsc,
        "peaks":         json.dumps(pk),
    }
