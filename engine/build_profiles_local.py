"""One-off local profile builder.

YouTube blocks Hugging Face's datacenter IP (bot detection), so the CLAP
profile embeddings can't be built on the Space. This script builds them here
on a residential IP and writes them under ``prebuilt_profiles/`` in the repo.
The app seeds DATA_DIR/profiles from there on startup, so the Space ships with
ready profiles and never has to touch YouTube for profile building.

Run from the engine/ directory:  python build_profiles_local.py
"""
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
OUT = _HERE / "prebuilt_profiles"
OUT.mkdir(exist_ok=True)

# Build straight into the prebuilt dir.
os.environ["DIGMORE_DATA"] = str(OUT.parent / "_localbuild")

import profiles  # noqa: E402


def cb(i, n, msg):
    try:
        print(f"  [{i}/{n}] {msg}")
    except Exception:
        print(f"  [{i}/{n}] (msg)")


def main():
    built = []
    for pid in profiles.PROFILES:
        print(f"\n=== Building '{pid}' ({profiles.PROFILES[pid]['label']}) ===")
        try:
            profiles.build_embedding(pid, progress_cb=cb)
            src_npy = profiles._emb_path(pid)
            src_json = profiles._meta_path(pid)
            (OUT / f"{pid}.npy").write_bytes(src_npy.read_bytes())
            (OUT / f"{pid}.json").write_text(
                src_json.read_text(encoding="utf-8"), encoding="utf-8")
            built.append(pid)
            print(f"  -> saved prebuilt_profiles/{pid}.npy + .json")
        except Exception as e:
            print(f"  !! FAILED {pid}: {e}")
    print(f"\nDone. Built: {built}")
    if len(built) != len(profiles.PROFILES):
        sys.exit(1)


if __name__ == "__main__":
    main()
