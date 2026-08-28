"""Non-interactive entrypoint for a GitHub Actions runner: generate one
DIGMORE candidate list for a profile and dump it as JSON.

PIVOT (28/08/2026): this does NOT run digger.py's YouTube+CLAP scoring —
tried it first (see git history: yt_newpipe.py, newpipe-cli/), and it fails
100% of the time on GitHub Actions runners, not because of a coding bug but
because YouTube's *watch/player-response* endpoint (StreamInfo.getInfo, the
step that turns a video id into an audio URL) hard-blocks these IPs with
SignInConfirmNotBotException ("Sign in to confirm you're not a bot") —
confirmed live, every single candidate. Crucially the *search* step (both
NewPipeExtractor's and the HTML-scrape fallback) works fine from these IPs;
it's specifically the player-response fetch that's gated, and that gate is
IP-reputation based, not client-library based — swapping yt-dlp for
NewPipeExtractor (the "jatz" approach) fixed the search half but cannot fix
this half from a GitHub-hosted IP. So there is no CLAP "vibe" score in this
pipeline: candidates are just deduplicated Discogs releases for the profile's
macrogenre (discogs_ext.GENRE_MAP), same year window (1969-83) and rating
gate as always. The real curation happens downstream, on the phone, which
has a legitimate residential/mobile IP: DigmoreViewModel searches each
candidate on Spotify and only keeps confident matches — that's also the step
that turns "found on Discogs" into "actually exists as a native Spotify
track", which was always necessary regardless of CLAP.

Usage:
    python generate_cli.py --profile jazz --target 25 --out out.json
"""
import argparse
import json
import sys
import time

import paths_boot  # noqa: F401
import discogs_ext


def collect_candidates(profile: str, target: int) -> list[dict]:
    """Pulls Discogs candidates for `profile` until there are at least
    `target * OVERSHOOT` distinct (artist, title) pairs, or rounds run out.
    Overshot on purpose: many obscure vinyl-only credits simply aren't on
    Spotify, and that filtering only happens later, on the phone."""
    OVERSHOOT = 4
    MAX_ROUNDS = 20
    want = max(target * OVERSHOOT, 40)

    seen_release_ids: set = set()
    seen_keys: set = set()
    results: list[dict] = []

    for rnd in range(1, MAX_ROUNDS + 1):
        if len(results) >= want:
            break
        cands, diag = discogs_ext.build_candidates(
            profile,
            exclude_ids=seen_release_ids,
            n_releases=18,
            tracks_per_release=4,
        )
        seen_release_ids.update(diag.get("release_ids", []))
        added = 0
        for c in cands:
            key = (c["artist"].strip().lower(), c["title"].strip().lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(c)
            added += 1
        print(f"round {rnd}: +{added} new (total {len(results)}/{want}), "
              f"{diag.get('releases_found', 0)} releases scanned", flush=True)
        if added == 0 and rnd > 3:
            # Pool for this profile/exclusion-set is drying up — stop rather
            # than spin through the remaining rounds for nothing.
            break

    return results[:want]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=list(discogs_ext.GENRE_MAP.keys()))
    ap.add_argument("--target", type=int, default=25)
    ap.add_argument("--out", default="out.json")
    args = ap.parse_args()

    t0 = time.time()
    results = collect_candidates(args.profile, args.target)
    print(f"status=done found={len(results)} elapsed={time.time() - t0:.1f}s", flush=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "profile": args.profile,
            "status": "done",
            "results": results,
        }, f, ensure_ascii=False, indent=2)

    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
