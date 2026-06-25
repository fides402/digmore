import json
import os
import random
import numpy as np

# Camelot code → root pitch class (C=0, C#=1, …, B=11)
CAMELOT_TO_PITCH = {
    "8B": 0, "9B": 7, "10B": 2, "11B": 9, "12B": 4,
    "1B": 11, "2B": 6, "3B": 1, "4B": 8, "5B": 3, "6B": 10, "7B": 5,
    "8A": 9, "9A": 4, "10A": 11, "11A": 6, "12A": 1,
    "1A": 8, "2A": 3, "3A": 10, "4A": 5, "5A": 0, "6A": 7, "7A": 2,
}


def _camelot_num(code: str) -> int:
    return int(code[:-1]) if code else 0


def semitones_to_target(source: str, target: str) -> int:
    """Minimal semitone shift to bring source root to target root."""
    s = CAMELOT_TO_PITCH.get(source, 0)
    t = CAMELOT_TO_PITCH.get(target, 0)
    diff = (t - s) % 12
    if diff > 6:
        diff -= 12
    return int(diff)


def _key_compat(src: str, tgt: str) -> float:
    if not src or not tgt:
        return 0.5
    s_n, t_n = _camelot_num(src), _camelot_num(tgt)
    s_m, t_m = src[-1], tgt[-1]
    if s_n == t_n:
        return 1.0  # same camelot position (relative major/minor)
    diff = min(abs(s_n - t_n), 12 - abs(s_n - t_n))
    if diff == 1 and s_m == t_m:
        return 0.90
    if diff == 1:
        return 0.75
    if diff == 2:
        return 0.55
    if diff == 3:
        return 0.40
    return max(0.1, 0.50 - diff * 0.07)


def _bpm_score(loop_bpm: float, master_bpm: float) -> float:
    if not loop_bpm or not master_bpm:
        return 0.5
    ratios = [loop_bpm / master_bpm, loop_bpm / master_bpm * 2, loop_bpm / master_bpm / 2]
    best = min(ratios, key=lambda r: abs(r - 1.0))
    dev = abs(best - 1.0)
    return float(max(0.1, 1.0 - dev * 1.8))


def _spectral_comp(loop_band: dict, existing_bands: list) -> float:
    if not existing_bands:
        return 1.0
    agg = {k: 0.0 for k in ("sub", "low", "mid", "high")}
    for b in existing_bands:
        for k in agg:
            agg[k] += b.get(k, 0.0)
    score = 0.0
    for k in agg:
        fill = min(1.0, agg[k])
        score += loop_band.get(k, 0.0) * (1.0 - fill)
    return float(min(1.0, score))


_PERC_TOKENS = frozenset({
    "drum", "break", "beat", "rim", "snare", "kick", "kicks", "hat", "hats",
    "cymbal", "clap", "claps", "perc", "groove", "bongo", "conga", "tom",
    "thumper", "tamb", "shaker", "cowbell", "crash", "hihat", "hi-hat",
    "batter", "boombap", "boom bap", "bap", "fill", "roll",
})

# Tokens that signal a true harmonic loop (chords / pads / progressions)
_HARMONIC_TOKENS = frozenset({
    "chord", "chords", "chord_", "progression", "prog", "comping", "comp",
    "pad", "pads", "harmony", "harmonic", "voicing",
    "piano", "rhodes", "keys", "organ", "clav",
    "strings", "string", "brass", "ensemble",
    "arp", "arpegg",
})

# Tokens that signal a single melodic phrase / stab (less preferred for harmony)
_PHRASE_TOKENS = frozenset({
    "phrase", "melody", "melod", "lead", "lick", "riff",
    "stab", "hit", "hook", "solo", "pluck", "note",
    "one shot", "oneshot", "1shot",
})

_MELODIC_TOKENS = frozenset({
    "piano", "keys", "synth", "pad", "chord", "melody", "melod", "guitar",
    "bass", "strings", "string", "brass", "horn", "flute", "sax", "rhodes",
    "organ", "arp", "lead", "vocal", "vox", "voice", "acapella",
})


def _harmonic_score_bonus(path: str) -> float:
    """Return a scoring bonus/malus based on how 'chordal' the filename looks.
    +0.18 for clear harmonic content, -0.12 for single-phrase/stab material."""
    fname  = os.path.basename(path).lower()
    folder = os.path.basename(os.path.dirname(path)).lower()
    combined = fname + " " + folder
    if any(tok in combined for tok in _HARMONIC_TOKENS):
        return 0.18
    if any(tok in combined for tok in _PHRASE_TOKENS):
        return -0.12
    return 0.0


def _path_is_percussive(path: str) -> bool:
    """Return True if the filename or immediate parent folder signals percussion."""
    fname  = os.path.basename(path).lower()
    folder = os.path.basename(os.path.dirname(path)).lower()
    # Melodic keyword in the filename overrides percussion folder name
    if any(tok in fname for tok in _MELODIC_TOKENS):
        return False
    return any(tok in fname or tok in folder for tok in _PERC_TOKENS)


def _bpm_prefilter(all_loops: list, master_bpm: float) -> list:
    """Drop loops whose BPM is incompatible even with half/double-time correction.
    Keeps loops with unknown BPM (None). Cuts the scoring loop from O(N) to O(compatible)."""
    if not master_bpm:
        return all_loops
    out = []
    for lp in all_loops:
        bpm = lp.get("bpm")
        if not bpm:
            out.append(lp)
            continue
        # Check direct, half-time, double-time ratios
        best = min(abs(bpm / master_bpm - 1.0),
                   abs(bpm * 0.5 / master_bpm - 1.0),
                   abs(bpm * 2.0 / master_bpm - 1.0))
        if best < 0.45:
            out.append(lp)
    return out


def find_complementary(seed_loop: dict, all_loops: list, n_per_role: int = 3,
                       pool_size: int = 10, temperature: float = 0.6) -> dict:
    """
    Returns {role: [n_per_role loop dicts enriched with _score, _semitones, _stretch_rate]}.

    Uses weighted-random selection from the top `pool_size` candidates per role so
    repeated calls produce varied combos while still favouring compatible material.
    `temperature` controls randomness: 0 = always top-1, 1 = fully random within pool.
    """
    master_camelot = seed_loop.get("camelot", "")
    master_bpm = seed_loop.get("bpm", 120.0) or 120.0
    seed_path = seed_loop.get("path", "")
    seed_band = json.loads(seed_loop.get("band_energy") or "{}") or {}

    # Pre-filter by BPM before the expensive per-loop scoring
    bpm_compatible = _bpm_prefilter(all_loops, master_bpm)

    results = {}
    for role in ("drums", "harmonic_melodic", "bass", "texture_fx"):
        candidates = []
        for loop in bpm_compatible:
            if loop["path"] == seed_path:
                continue
            if loop.get("role") not in (role, "full"):
                continue

            drum_sc = loop.get("drum_score") or 0.0

            # Hard gates to keep roles clean.
            # Melodic pool: exclude percussive drum_score OR percussive path name.
            # Drums pool:   exclude clearly melodic material.
            if role == "harmonic_melodic":
                if drum_sc > 0.30:
                    continue
                if _path_is_percussive(loop["path"]):
                    continue
                bars = loop.get("bars")
                dur  = loop.get("duration") or 0.0
                # Single-bar loops are one chord / one stab — not a progression.
                # bars=None from LoopMatcher: use duration as proxy (< 5s = likely stab).
                if bars is not None and bars < 2:
                    continue
                if bars is None and dur < 5.0:
                    continue
            if role == "drums" and drum_sc < 0.45:
                continue

            loop_band = json.loads(loop.get("band_energy") or "{}") or {}
            semitones = semitones_to_target(loop.get("camelot", master_camelot), master_camelot)
            stretch_rate = (loop.get("bpm") or master_bpm) / master_bpm

            key_sc = _key_compat(loop.get("camelot", ""), master_camelot) if role != "drums" else 0.8
            bpm_sc = _bpm_score(loop.get("bpm"), master_bpm)
            spec_sc = _spectral_comp(loop_band, [seed_band])
            semi_penalty = max(0.0, 1.0 - abs(semitones) * 0.10)
            drum_bonus = drum_sc if role == "drums" else 0.0
            harmonic_bonus = _harmonic_score_bonus(loop["path"]) if role == "harmonic_melodic" else 0.0

            score = (
                key_sc * 0.35
                + bpm_sc * 0.30
                + spec_sc * 0.15
                + semi_penalty * 0.10
                + drum_bonus * 0.10
                + harmonic_bonus
            )

            candidates.append({
                **loop,
                "_score": score,
                "_semitones": semitones,
                "_stretch_rate": stretch_rate,
            })

        candidates.sort(key=lambda x: x["_score"], reverse=True)
        pool = candidates[:max(pool_size, n_per_role)]

        if len(pool) <= n_per_role:
            results[role] = pool
            continue

        # Weighted-random sampling without replacement.
        # Weight = score^(1/temperature): higher temp → flatter → more random.
        scores = np.array([c["_score"] for c in pool], dtype=float)
        weights = np.power(scores, 1.0 / max(temperature, 0.01))
        weights /= weights.sum()
        chosen_idx = np.random.choice(len(pool), size=n_per_role, replace=False, p=weights)
        results[role] = [pool[i] for i in sorted(chosen_idx)]

    return results
