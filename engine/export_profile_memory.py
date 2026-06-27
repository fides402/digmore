"""Esporta TUTTA la memoria dei profili musicali (soul/jazz/ost) in un unico JSON.

Unisce, per ciascun profilo:
  - reference_tracks : tracce seed che definiscono il profilo
  - embedding        : vettore CLAP (512 dim) del profilo
  - listening_events : storico d'ascolto affinato (taste) filtrato per profilo
  - seen_videos      : id YouTube gia' proposti per quel profilo

Uso:  python export_profile_memory.py [output.json]
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

import paths_boot  # noqa: F401  (mette engine_libs + DATA_DIR su sys.path)
from paths_boot import DATA_DIR
import profiles

PREBUILT = Path(__file__).resolve().parent / "prebuilt_profiles"


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def build() -> dict:
    taste_log = _load_json(DATA_DIR / "taste_log.json", [])

    out = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "DIGMORE",
        "profiles": {},
    }

    for pid, meta in profiles.PROFILES.items():
        ref = _load_json(PREBUILT / f"{pid}.json", {})
        emb_path = PREBUILT / f"{pid}.npy"
        embedding = (
            np.load(emb_path).astype(float).round(6).tolist()
            if emb_path.exists() else None
        )
        seen = _load_json(DATA_DIR / f"seen_videos_{pid}.json", [])
        events = [e for e in taste_log if e.get("profile") == pid]

        out["profiles"][pid] = {
            "label": meta["label"],
            "n_reference_tracks": ref.get("n_tracks", 0),
            "reference_tracks": ref.get("tracks", []),
            "embedding_dim": len(embedding) if embedding else 0,
            "embedding": embedding,
            "n_listening_events": len(events),
            "listening_events": events,
            "n_seen_videos": len(seen),
            "seen_videos": seen,
        }

    return out


def main():
    dst = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parents[1] / "digmore_profile_memory.json")
    data = build()
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Scritto: {dst}")
    for pid, p in data["profiles"].items():
        print(f"  {pid:5s} ({p['label']}): "
              f"{p['n_reference_tracks']} ref · "
              f"{p['n_listening_events']} ascolti · "
              f"{p['n_seen_videos']} visti · "
              f"emb {p['embedding_dim']}d")


if __name__ == "__main__":
    main()
