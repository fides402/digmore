"""Taste fine-tuning — anchored, reversible listening profile.

DIGMORE genera contro un profilo CLAP fisso (P0, costruito dai CSV reference).
Questo modulo aggiunge un *affinamento* di quel profilo basato su COSA ASCOLTI
DAVVERO, con tre garanzie di sicurezza:

  1. ANCORA FISSA: lo scostamento è sempre misurato dal profilo originale P0,
     mai dallo stato precedente → niente deriva cumulativa.
  2. GUINZAGLIO: il profilo affinato può inclinarsi al massimo di THETA_MAX gradi
     da P0. Oltre quel cono non va, qualunque cosa tu ascolti.
  3. REVERSIBILE: un interruttore globale. OFF → si torna esattamente a P0.

Il segnale viene SOLO da brani effettivamente riprodotti:
  - skip secco (<5s o <8%)            → negativo forte
  - ascolto pieno / % alta            → positivo (cresce con la %)
  - brano lungo ascoltato a lungo     → positivo via PAVIMENTO ASSOLUTO in secondi
  - riascolti                         → amplifica il positivo

Costruisce due zone nello spazio embedding (stesso di P0):
  P (apprezzato) attira, N (non apprezzato) respinge — in modo ASIMMETRICO
  (P forte e veloce, N debole, lento e a soglia di densità).

Tutto resta LOCALE: log e cache embedding non escono mai via API pubbliche
né finiscono nella libreria sincronizzata.
"""
import json
import math
import threading
import time
from pathlib import Path

import numpy as np

import paths_boot  # noqa: F401
from paths_boot import DATA_DIR

# ── File locali (privati) ─────────────────────────────────────────────────────
_LOG_PATH   = DATA_DIR / "taste_log.json"      # eventi d'ascolto grezzi
_EMB_PATH   = DATA_DIR / "taste_emb.npz"        # video_id → embedding CLAP (512,)
_STATE_PATH = DATA_DIR / "taste_state.json"     # {"active": bool}

# ── Parametri (tarati per effetto VISIBILE e REVERSIBILE) ─────────────────────
MIN_POS      = 6        # ascolti positivi minimi prima di toccare il ranking
MIN_NEG      = 3        # densità minima negativi prima che N respinga
THETA_MAX    = 28.0     # guinzaglio: scostamento max da P0, in gradi
ALPHA        = 0.65     # attrazione verso la zona positiva
BETA         = 0.30     # repulsione dalla zona negativa (ASIMMETRICO: < ALPHA)
HALFLIFE_D   = 14.0     # emivita recency: ascolti recenti pesano di più
ABS_FLOOR_S  = 60.0     # sotto questi secondi il pavimento assoluto non conta
ABS_SAT_S    = 150.0    # a questi secondi il pavimento assoluto è pieno
NEUTRAL_PCT  = 0.25     # sotto questa % l'ascolto è neutro/negativo
SKIP_SEC     = 5.0      # sotto questi secondi = skip secco
SKIP_PCT     = 0.08

_lock = threading.Lock()
_emb_cache: dict[str, np.ndarray] | None = None


# ── Stato (interruttore) ──────────────────────────────────────────────────────

def is_active() -> bool:
    try:
        return bool(json.loads(_STATE_PATH.read_text(encoding="utf-8")).get("active", False))
    except Exception:
        return False


def set_active(active: bool) -> bool:
    _STATE_PATH.write_text(json.dumps({"active": bool(active)}), encoding="utf-8")
    return bool(active)


# ── Log eventi d'ascolto ──────────────────────────────────────────────────────

def _read_log() -> list[dict]:
    try:
        return json.loads(_LOG_PATH.read_text(encoding="utf-8")) if _LOG_PATH.exists() else []
    except Exception:
        return []


def log_event(video_id: str, listened_sec: float, duration_sec: float, profile: str = ""):
    """Registra un evento d'ascolto con il profilo attivo.
    Il profilo è obbligatorio per evitare che segnali soul/jazz/ost si incrocino.
    Sempre attivo (anche con tuning OFF): la memoria si accumula in sottofondo."""
    if not video_id or duration_sec <= 0:
        return
    ev = {"vid": video_id, "sec": round(float(listened_sec), 1),
          "dur": round(float(duration_sec), 1), "ts": time.time(),
          "profile": profile or ""}
    with _lock:
        log = _read_log()
        log.append(ev)
        if len(log) > 5000:
            log = log[-5000:]
        _LOG_PATH.write_text(json.dumps(log), encoding="utf-8")


def reset():
    """Azzera la memoria d'ascolto (non tocca i profili di partenza)."""
    global _emb_cache
    with _lock:
        for p in (_LOG_PATH, _EMB_PATH):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        _emb_cache = None


# ── Cache embedding dei candidati ─────────────────────────────────────────────

def _load_emb() -> dict[str, np.ndarray]:
    global _emb_cache
    if _emb_cache is not None:
        return _emb_cache
    cache: dict[str, np.ndarray] = {}
    if _EMB_PATH.exists():
        try:
            with np.load(str(_EMB_PATH)) as z:
                cache = {k: z[k].astype(np.float32) for k in z.files}
        except Exception:
            cache = {}
    _emb_cache = cache
    return cache


def cache_embedding(video_id: str, vec):
    """Salva l'embedding CLAP (512,) di un candidato, keyed by video_id.
    Serve perché un brano ascoltato possa rientrare nello stesso spazio di P0."""
    if not video_id or vec is None:
        return
    v = np.asarray(vec, dtype=np.float32)
    if v.ndim == 2:
        v = v.mean(axis=0)
    v = v / (np.linalg.norm(v) + 1e-9)
    with _lock:
        cache = _load_emb()
        if video_id in cache:
            return
        cache[video_id] = v
        try:
            np.savez_compressed(str(_EMB_PATH), **cache)
        except Exception:
            pass


# ── Segnale d'ascolto (engagement) ────────────────────────────────────────────

def engagement(listened_sec: float, duration_sec: float) -> float:
    """Peso in [-1, +1] da un singolo evento d'ascolto."""
    if duration_sec <= 0:
        return 0.0
    pct = listened_sec / duration_sec
    if listened_sec < SKIP_SEC or pct < SKIP_PCT:
        return -1.0
    s_rel = (pct - NEUTRAL_PCT) / (1.0 - NEUTRAL_PCT)          # negativo se pct<neutro
    s_abs = (listened_sec - ABS_FLOOR_S) / (ABS_SAT_S - ABS_FLOOR_S)
    s_abs = max(0.0, min(1.0, s_abs))                          # pavimento assoluto
    e = max(s_rel, s_abs)                                       # vince il più forte
    return max(-1.0, min(1.0, e))


def _recency(ts: float, now: float) -> float:
    age_days = max(0.0, (now - ts) / 86400.0)
    return 0.5 ** (age_days / HALFLIFE_D)


# ── Costruzione zone P / N ────────────────────────────────────────────────────

def _build_zones(profile: str = ""):
    """Aggrega il log PER BRANO filtrato per profilo.
    Così soul/jazz/ost non si inquinano a vicenda.
    Ritorna (P, N, pos_count, neg_count) con P/N normalizzati o None."""
    log = _read_log()
    emb = _load_emb()
    if not log or not emb:
        return None, None, 0, 0

    # filtra per profilo se specificato
    if profile:
        log = [ev for ev in log if ev.get("profile", "") == profile]
    if not log:
        return None, None, 0, 0

    now = time.time()
    groups: dict[str, list[dict]] = {}
    for ev in log:
        groups.setdefault(ev["vid"], []).append(ev)

    P_sum = None; N_sum = None
    pos_count = 0; neg_count = 0
    for vid, evs in groups.items():
        if vid not in emb:
            continue
        es = [engagement(ev["sec"], ev["dur"]) for ev in evs]
        best = max(es)
        # rappresentativo: se c'è almeno un positivo prendi il migliore,
        # altrimenti il negativo peggiore
        e = best if best > 0 else min(es)
        rec = max(_recency(ev["ts"], now) for ev in evs)
        v = emb[vid]
        if e > 0:
            n_pos = sum(1 for x in es if x > 0)
            w = rec * e * (1.0 + 0.5 * min(n_pos - 1, 4))       # replay amplifica
            P_sum = w * v if P_sum is None else P_sum + w * v
            pos_count += 1
        elif e < 0:
            w = rec * (-e)
            N_sum = w * v if N_sum is None else N_sum + w * v
            neg_count += 1

    P = None
    if P_sum is not None and pos_count > 0:
        P = (P_sum / (np.linalg.norm(P_sum) + 1e-9)).astype(np.float32)
    N = None
    if N_sum is not None and neg_count >= MIN_NEG:
        N = (N_sum / (np.linalg.norm(N_sum) + 1e-9)).astype(np.float32)
    return P, N, pos_count, neg_count


def _leash(target: np.ndarray, p0: np.ndarray, max_deg: float) -> np.ndarray:
    """Proietta `target` dentro un cono di ampiezza max_deg attorno a P0 (fisso).
    Se è già dentro, lo lascia; altrimenti slerp fino al bordo del cono."""
    t = target / (np.linalg.norm(target) + 1e-9)
    p = p0 / (np.linalg.norm(p0) + 1e-9)
    cos = float(np.clip(np.dot(t, p), -1.0, 1.0))
    ang = math.degrees(math.acos(cos))
    if ang <= max_deg or ang < 1e-6:
        return t
    frac = max_deg / ang
    omega = math.acos(cos)
    so = math.sin(omega)
    if so < 1e-9:
        return p
    out = (math.sin((1 - frac) * omega) / so) * p + (math.sin(frac * omega) / so) * t
    return (out / (np.linalg.norm(out) + 1e-9)).astype(np.float32)


def tuned_profile(p0: np.ndarray, profile: str = ""):
    """Ritorna (profilo_affinato, info). Se non c'è abbastanza segnale,
    ritorna P0 invariato (effetto nullo). `info` per la trasparenza."""
    p0 = np.asarray(p0, dtype=np.float32)
    P, N, pos, neg = _build_zones(profile)
    info = {"positives": pos, "negatives": neg, "deviation_deg": 0.0,
            "neg_active": False, "ready": pos >= MIN_POS}
    if P is None or pos < MIN_POS:
        return p0, info

    target = p0 + ALPHA * (P - p0)
    if N is not None:
        target -= BETA * (N - p0)
        info["neg_active"] = True
    tuned = _leash(target, p0, THETA_MAX)

    cos = float(np.clip(np.dot(tuned, p0 / (np.linalg.norm(p0) + 1e-9)), -1.0, 1.0))
    info["deviation_deg"] = round(math.degrees(math.acos(cos)), 1)
    return tuned, info


def negative_centroid(profile: str = ""):
    """Solo la zona negativa (per la repulsione nel ranking), o None."""
    _, N, _, _ = _build_zones(profile)
    return N


def neg_confidence(profile: str = "") -> float:
    """Confidenza nel segnale negativo ∈ [0, 1].

    Bassa (< 0.4)  → zona incerta, forse falsi negativi → re-proba spesso.
    Alta  (> 0.85) → molti skip coerenti → zona soppressa, re-proba raramente.

    Formula: 1 − e^(−skip_count / 3).
    Con 1 skip: ~0.28. Con 3: ~0.63. Con 7+: ~0.90.
    """
    log = _read_log()
    if not log:
        return 0.0
    if profile:
        log = [ev for ev in log if ev.get("profile", "") == profile]
    if not log:
        return 0.0
    emb = _load_emb()
    now = time.time()
    groups: dict[str, list] = {}
    for ev in log:
        groups.setdefault(ev["vid"], []).append(ev)

    total_skips = 0
    for vid, evs in groups.items():
        if vid not in emb:
            continue
        es = [engagement(ev["sec"], ev["dur"]) for ev in evs]
        total_skips += sum(1 for e in es if e < 0)

    return 1.0 - math.exp(-total_skips / 3.0)


def status(profile: str = "") -> dict:
    log = _read_log()
    P, N, pos, neg = _build_zones(profile)
    conf = neg_confidence(profile)
    reproba_slots = 0 if conf >= 0.85 else (2 if conf < 0.40 else 1)
    return {
        "active": is_active(),
        "events": len(log),
        "positives": pos,
        "negatives": neg,
        "ready": pos >= MIN_POS,
        "neg_active": N is not None,
        "neg_confidence": round(conf, 2),
        "reproba_slots": reproba_slots,
        "min_pos": MIN_POS,
    }
