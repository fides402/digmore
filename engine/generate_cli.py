"""Non-interactive entrypoint for a GitHub Actions runner: generate one
DIGMORE playlist for a profile and dump the results as JSON.

Same engine as app.py's POST /api/generate (digger.run over profiles.py
embeddings), synchronous (no job store, no polling).

Uses yt_hunter.py (yt-dlp), NOT yt_newpipe.py/newpipe-cli — tried the latter
first (see git history), but a homemade "just attach a Cookie header" login
in Kotlin/NewPipeExtractor isn't enough to lift YouTube's
SignInConfirmNotBotException on the watch/player-response endpoint: tested
live with real cookies, still blocked on every candidate. Google's
authenticated endpoints need a proper SAPISIDHASH Authorization header
derived from the cookies, which yt-dlp already implements correctly (it's a
mature, actively maintained cookie-auth implementation) — yt_hunter.py's
own YT_COOKIES_FILE mechanism (built for the exact same HF Spaces
datacenter-IP block, see profiles.py) just needed to actually be tried on a
GitHub Actions runner, which nobody had done before switching to
NewPipeExtractor. Needs Node.js on the runner (yt-dlp's `js_runtimes`
challenge solver, already configured in yt_hunter.py's YoutubeDL() wrapper)
and ffmpeg on PATH — see .github/workflows/digmore-generate.yml.

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
