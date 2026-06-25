"""Weighted similarity between the reference sample and a YouTube candidate.

Key design choices:
  - Raw cosine similarity (not the +1/2 trick) → wider, more discriminating range
  - Batch normalization after collecting all candidates → best match always stands out
  - Key compatibility as multiplicative bonus on harmonic axes

Weights (sum = 1.0):
  0.55  CLAP              overall vibe / style / instrumentation  ← dominant
  0.15  MFCCs             timbre / instrument texture
  0.12  Chroma            harmonic color (transpose-invariant)
  0.08  Tonnetz           tonal centroid / harmonic relations
  0.05  Spectral contrast  instrument clarity per band
  0.03  Spectral vector    brightness / energy profile
  0.02  Tempogram         rhythmic periodicity / groove

Candidate vectors may be 2-D (n_segments × dim): every similarity max-pools
over segments so the best-matching window of a full song wins.
"""
import numpy as np

from features import key_compat

W = {
    "clap":     0.55,
    "mfcc":     0.15,
    "chroma":   0.12,
    "tonnetz":  0.08,
    "contrast": 0.05,
    "spec":     0.03,
    "tempo":    0.02,
}


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    """Raw cosine similarity, clipped to [0, 1]; max-pooled over segments.

    Vectors are L2-normalized, so a dot product is the cosine. When the
    candidate is 2-D (one row per analysis window), we take the best window.
    We clip (not shift) because the feature vectors are non-negative, so raw
    cosine already spans [0, 1] far more discriminatingly than (dot+1)/2.
    """
    if a is None or b is None:
        return 0.0
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if b.ndim == 2:
        return float(np.clip((b @ a).max(), 0.0, 1.0))
    if a.ndim == 2:
        return float(np.clip((a @ b).max(), 0.0, 1.0))
    return float(np.clip(np.dot(a, b), 0.0, 1.0))


def _chroma_sim(ref: np.ndarray, cand: np.ndarray) -> float:
    """Transpose-invariant chroma similarity.

    A sample is often flipped pitched up/down, so the same chord progression
    lands on different pitch classes. We rotate the 12-bin chroma over all 12
    semitone shifts (and over candidate segments) and keep the best alignment.
    np.roll is a permutation, so the L2 norm — and thus the cosine bound — holds.
    """
    if ref is None or cand is None:
        return 0.0
    ref = np.asarray(ref, dtype=np.float32)
    cand = np.asarray(cand, dtype=np.float32)
    if cand.ndim == 1:
        cand = cand[None, :]
    best = 0.0
    for row in cand:
        for s in range(12):
            best = max(best, float(np.dot(ref, np.roll(row, s))))
    return float(np.clip(best, 0.0, 1.0))


def score(sample: dict, cand: dict) -> dict:
    """Compute per-component similarity scores (not yet batch-normalized)."""
    clap     = _cos(sample.get("clap"),          cand.get("clap"))
    mfcc     = _cos(sample.get("mfcc_mean"),     cand.get("mfcc_mean"))
    chroma   = _chroma_sim(sample.get("chroma_mean"), cand.get("chroma_mean"))
    tonnetz  = _cos(sample.get("tonnetz_mean"),  cand.get("tonnetz_mean"))
    contrast = _cos(sample.get("contrast_mean"), cand.get("contrast_mean"))
    spec     = _cos(sample.get("spec_vec"),      cand.get("spec_vec"))
    tempo    = _cos(sample.get("tempogram_mean"), cand.get("tempogram_mean"))

    # Harmonic bonus: boosts chroma+tonnetz when keys are compatible
    kb = key_compat(sample.get("camelot", ""), cand.get("camelot", ""))
    chroma_adj  = chroma  * (0.6 + 0.4 * kb)
    tonnetz_adj = tonnetz * (0.6 + 0.4 * kb)

    total = (
        W["clap"]     * clap       +
        W["mfcc"]     * mfcc       +
        W["chroma"]   * chroma_adj +
        W["tonnetz"]  * tonnetz_adj +
        W["contrast"] * contrast   +
        W["spec"]     * spec       +
        W["tempo"]    * tempo
    )
    return {
        "score":     round(total, 4),
        "clap":      round(clap, 3),
        "mfcc":      round(mfcc, 3),
        "chroma":    round(chroma, 3),
        "tonnetz":   round(tonnetz, 3),
        "contrast":  round(contrast, 3),
        "spec":      round(spec, 3),
        "tempo":     round(tempo, 3),
        "key_bonus": round(kb, 3),
    }


def batch_normalize(results: list[dict]) -> list[dict]:
    """Rescale each score component relative to the batch distribution.

    Requires at least 2 results with meaningful spread to be useful.
    With 0-1 results, or when all values are identical, returns raw scores
    unchanged so the UI shows real numbers instead of a meaningless 50%.
    """
    if len(results) < 2:
        return results   # nothing to normalize against — keep raw scores

    axes = ["clap", "mfcc", "chroma", "tonnetz", "contrast", "spec", "tempo"]
    for ax in axes:
        vals = [r[ax] for r in results if ax in r]
        if not vals:
            continue
        mn, mx = min(vals), max(vals)
        spread = mx - mn
        if spread < 0.01:
            continue   # all identical on this axis — don't flatten to 0.5
        for r in results:
            if ax in r:
                r[ax] = round((r[ax] - mn) / spread, 3)

    # Recompute total with key_bonus
    for r in results:
        kb = r.get("key_bonus", 0.5)
        chroma_adj  = r.get("chroma",  0.5) * (0.6 + 0.4 * kb)
        tonnetz_adj = r.get("tonnetz", 0.5) * (0.6 + 0.4 * kb)
        r["score"] = round(
            W["clap"]     * r.get("clap",     0.5) +
            W["mfcc"]     * r.get("mfcc",     0.5) +
            W["chroma"]   * chroma_adj              +
            W["tonnetz"]  * tonnetz_adj              +
            W["contrast"] * r.get("contrast", 0.5) +
            W["spec"]     * r.get("spec",     0.5) +
            W["tempo"]    * r.get("tempo",    0.5),
            4
        )
    return results
