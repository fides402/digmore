"""Non-interactive entrypoint for a GitHub Actions runner: generate one
DIGMORE playlist for a profile and dump the results as JSON.

Same engine as app.py's POST /api/generate (digger.run over profiles.py
embeddings), but synchronous (no job store, no polling) and using
yt_newpipe.py instead of yt_hunter.py for the per-candidate YouTube step —
see yt_newpipe.py's docstring for why (datacenter-IP bot-check).

Usage:
    python generate_cli.py --profile jazz --target 30 --out out.json
"""
import argparse
import json
import sys
import time

import paths_boot  # noqa: F401
import profiles
import digger
import yt_newpipe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=list(profiles.PROFILES.keys()))
    ap.add_argument("--target", type=int, default=25)
    ap.add_argument("--out", default="out.json")
    args = ap.parse_args()

    emb = profiles.get_embedding(args.profile)
    if emb is None:
        print(f"profile '{args.profile}' has no prebuilt embedding", file=sys.stderr)
        return 1

    job: dict = {}
    t0 = time.time()

    def _progress(res: dict):
        print(f"[{time.time() - t0:6.1f}s] accepted: {res.get('artist')} - {res.get('title')} "
              f"(vibe {res.get('vibe')})", flush=True)

    digger.run(
        job=job,
        profile=args.profile,
        profile_emb=emb,
        target=args.target,
        yt_module=yt_newpipe,
        on_track_accepted=_progress,
    )

    status = job.get("status")
    results = job.get("results", [])
    print(f"status={status} accepted={len(results)} analyzed={job.get('analyzed')} "
          f"elapsed={time.time() - t0:.1f}s", flush=True)

    skipped = job.get("skipped", [])
    if skipped:
        print(f"skipped (exceptions during evaluation): {len(skipped)}", file=sys.stderr)
        for s in skipped[:15]:
            print(f"  - {s}", file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "profile": args.profile,
            "status": status,
            "results": results,
        }, f, ensure_ascii=False, indent=2)

    return 0 if status == "done" else (0 if results else 1)


if __name__ == "__main__":
    raise SystemExit(main())
