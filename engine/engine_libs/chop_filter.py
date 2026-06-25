"""Onset-guided window detection for SampleHunter candidates.

Adapts the core logic from sampleChop-ui (C:/Users/User/Downloads/sampleChop-ui)
to return analysis windows instead of WAV files. Used as an alternative to
fixed-interval segmentation: instead of 3 arbitrary 35s slices, we find the
N strongest musical transition points (onset peaks) and analyse those, which
are far more likely to contain the actual sampleable section.

The pkl neural network (nn32_16_8_4.pkl) in sampleChop-ui is incompatible with
the current sklearn version and is unused in its UI code too — ignored here.
"""
import numpy as np
import librosa

_SR         = 22050      # must match features.ANALYSIS_SR
_HOP        = 512
_SENSITIVITY = 0.70     # same default as sampleChop-ui
_N_WINDOWS   = 8        # max windows to select
_MIN_SEG_S   = 2.0      # skip segments shorter than this
_MAX_SEG_S   = 35.0     # cap each window at this length (matches _SEG_LEN)
_WAIT_S      = 0.10     # min gap between onsets (100ms, same as sampleChop-ui)


def find_sampleable_windows(
    y_mono: np.ndarray,
    sr: int,
    sensitivity: float = _SENSITIVITY,
    n_windows: int = _N_WINDOWS,
    harmonic: bool = True,
) -> list[dict]:
    """Detect the N strongest onset windows in a candidate audio signal.

    Returns a list of dicts sorted by onset strength (descending):
        {start_sec, end_sec, strength}

    Falls back to an empty list if no onsets are found (caller should use
    fixed-interval fallback).

    Parameters mirror sampleChop-ui:
      sensitivity  0.1 = few cuts (only very strong onsets)
                   0.99 = many cuts (very sensitive)
      harmonic     run HPSS harmonic extraction before onset detection so
                   drum transients don't dominate the onset picks
    """
    if len(y_mono) == 0:
        return []

    # Optional: run onset detection on harmonic component so drums don't
    # dominate the picked moments (identical to sampleChop-ui harmonic option).
    if harmonic:
        try:
            y_proc = librosa.effects.harmonic(y_mono, margin=3.0)
        except Exception:
            y_proc = y_mono
    else:
        y_proc = y_mono

    # delta = 1.0 - sensitivity  (sampleChop-ui convention)
    wait_frames = max(1, int(sr * _WAIT_S / _HOP))
    try:
        onset_frames = librosa.onset.onset_detect(
            y=y_proc, sr=sr, hop_length=_HOP,
            backtrack=True,
            delta=1.0 - sensitivity,
            wait=wait_frames,
        )
    except Exception:
        return []

    if len(onset_frames) == 0:
        return []

    # Score each onset by its envelope strength.
    onset_env  = librosa.onset.onset_strength(y=y_mono, sr=sr, hop_length=_HOP)
    n_env      = len(onset_env)
    frame_scores = [
        (int(f), float(onset_env[min(int(f), n_env - 1)]))
        for f in onset_frames
    ]

    # Select top N: take 2×N by strength, then subsample to N chronologically.
    if len(frame_scores) > n_windows:
        top = sorted(frame_scores, key=lambda x: x[1], reverse=True)[: n_windows * 2]
        top_chron = sorted(top, key=lambda x: x[0])
        if len(top_chron) > n_windows:
            step = len(top_chron) / n_windows
            selected = [top_chron[int(i * step)] for i in range(n_windows)]
        else:
            selected = top_chron
    else:
        selected = frame_scores

    selected = sorted(set(selected), key=lambda x: x[0])

    # Build windows: from each onset to the next onset (or end), capped at MAX_SEG_S.
    total_sec = len(y_mono) / sr
    windows   = []
    for i, (frame, strength) in enumerate(selected):
        start_s = float(librosa.frames_to_time(frame, sr=sr, hop_length=_HOP))
        if i + 1 < len(selected):
            next_s = float(librosa.frames_to_time(selected[i + 1][0], sr=sr, hop_length=_HOP))
        else:
            next_s = total_sec
        end_s = min(next_s, start_s + _MAX_SEG_S)

        if end_s - start_s < _MIN_SEG_S:
            continue

        windows.append({
            "start_sec": round(start_s, 3),
            "end_sec":   round(end_s,   3),
            "strength":  round(strength, 4),
        })

    # Return sorted by onset strength so the caller can pick the best first.
    windows.sort(key=lambda w: w["strength"], reverse=True)
    return windows


def filter_sampleable(
    y_mono: np.ndarray,
    sr: int,
    sensitivity: float = _SENSITIVITY,
    n_windows: int = _N_WINDOWS,
    strength_factor: float = 1.5,
) -> list[dict]:
    """Return only windows that are genuinely 'sampleable'.

    A window is sampleable if its onset strength >= strength_factor × mean
    onset strength of the whole snippet. Windows below this threshold are too
    uniform / quiet / uninteresting to be a useful sample.

    strength_factor:
        1.0  → at least average energy (loose gate)
        1.5  → 50% above average (default — filters bland sections)
        2.0  → clearly a musical event / break / entry point

    Returns [] if no window passes the gate. Caller should skip the candidate.
    Each returned window: {start_sec, end_sec, strength, strength_ratio}
    """
    if len(y_mono) == 0:
        return []

    # Global baseline: mean onset strength of the full snippet.
    global_onset_env = librosa.onset.onset_strength(y=y_mono, sr=sr, hop_length=_HOP)
    global_mean = float(np.mean(global_onset_env)) + 1e-9

    # Detect windows using harmonic onset detection (same as sampleChop-ui).
    windows = find_sampleable_windows(
        y_mono, sr,
        sensitivity=sensitivity,
        n_windows=n_windows,
        harmonic=True,
    )

    # Apply the strength gate.
    gate = global_mean * strength_factor
    passed = []
    for w in windows:
        ratio = w["strength"] / global_mean
        if w["strength"] >= gate:
            passed.append({**w, "strength_ratio": round(ratio, 2)})

    return passed
