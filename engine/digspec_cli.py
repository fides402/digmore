"""Discogs-only generator driven by a DigSpec sent from the phone.

This is the cloud half of DIGMORE v2 (see E:\\diggaplayer\\PIANO_DIGMORE_V2.md).
The dig itself is unchanged — it is still DIGMORE: obscure Discogs releases,
popularity-capped, rating-gated, Last.fm fame-filtered. What changed is where
the *profile* comes from. Instead of one of three fixed macrogenres, the phone
sends the artists that actually make up a slice of the user's taste (a cluster
of their liked songs in CLAP embedding space, or the contents of a Spotify
link) plus the era those records come from, and this script works out what to
ask Discogs for.

DISCOGS ONLY. No YouTube and no CLAP here, for the reason documented at length
in generate_cli.py and the workflow: YouTube refuses this IP class outright,
even with a valid authenticated cookie export, so the audio side runs on the
phone. The CLAP ranking of these candidates happens there too, against the
profile's own centroid — this script never sees it.

Input spec (JSON, base64-encoded, from the `spec` workflow input):

    {"id": "cluster-2",                    # optional, echoed back
     "seed_artists": ["Piero Umiliani", ...],
     "year_from": 1966, "year_to": 1979,   # optional; wins over derived
     "searches": [["Jazz", "Modal"], ...], # optional; skips derivation
     "countries": ["Italy"],               # optional; wins over derived
     "target": 30}                         # optional, default 30

Usage:
    python digspec_cli.py --spec <base64> --out out.json
"""
import argparse
import base64
import json
import sys

import paths_boot  # noqa: F401  -- puts engine_libs on sys.path
import artist_axes
import discogs_ext

# Discogs' search filters the year window client-side over a fixed number of
# sampled pages, so a narrow window does not fail loudly — it just returns
# very little, which reads exactly like "this style is rare". Anything under
# this many years gets padded symmetrically before the first round.
MIN_WINDOW_YEARS = 10

# Overshoot: the "measured: 66 of 100" Spotify hit rate below was from
# mainstream-leaning catalogue. On the niche repertoire this axis-restricted
# digger actually targets, a real run found only 7 of 61 Discogs candidates
# (~11%) on Spotify (Bala Wala Chi run, 04/09/2026 — see HANDOFF.md). DIGMORE
# v2 then RANKS whatever lands by CLAP similarity, so it needs real margin to
# choose from: ~10x the target, up from 3x. Cheap to raise — unmatched
# candidates cost only a Spotify search (~1s), only matched ones pay the
# embedding.
OVERSHOOT = 10
MAX_ROUNDS = 14


def _decode(spec_b64: str) -> dict:
    raw = base64.b64decode(spec_b64 + "=" * (-len(spec_b64) % 4))
    return json.loads(raw.decode("utf-8"))


def _normalize_searches(raw) -> list[dict]:
    """Accepts [["Jazz","Modal"], ...] (the phone's tuple form) or
    [{"genre":...,"style":...}, ...], and drops entries that are all empty."""
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            g, s = item.get("genre", ""), item.get("style", "")
        elif isinstance(item, (list, tuple)):
            g = item[0] if len(item) > 0 else ""
            s = item[1] if len(item) > 1 else ""
        else:
            continue
        if g or s:
            out.append({"genre": g, "style": s})
    return out


def _widen(lo: int, hi: int, by: int) -> tuple[int, int]:
    return max(1900, lo - by), min(2035, hi + by)


def collect(searches, countries, year_from, year_to, target):
    """Deduplicated candidates for these axes, plus how it went."""
    want = max(target * OVERSHOOT, 45)
    seen_release_ids: set = set()
    seen_keys: set = set()
    results: list[dict] = []
    rounds = 0
    widened = False

    while rounds < MAX_ROUNDS and len(results) < want:
        rounds += 1
        cands, diag = discogs_ext.build_candidates(
            "_digspec",                      # not in GENRE_MAP: searches= wins
            searches=searches,
            countries=countries,
            year_from=year_from,
            year_to=year_to,
            exclude_ids=seen_release_ids,
            n_releases=18,
            tracks_per_release=4,
        )
        seen_release_ids.update(diag.get("release_ids", []))
        added = 0
        for c in cands:
            key = ((c.get("artist") or "").strip().lower(),
                   (c.get("title") or "").strip().lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(c)
            added += 1
        print(f"round {rounds}: +{added} new (total {len(results)}/{want}), "
              f"{diag.get('releases_found', 0)} releases scanned", flush=True)

        # A window that is too tight is the single most likely reason for a
        # thin round, and it is indistinguishable from "rare style" in the
        # numbers — so widen once, explicitly, and say so in the output rather
        # than letting the run look merely unlucky.
        if not widened and rounds >= 3 and len(results) < target:
            year_from, year_to = _widen(year_from, year_to, 5)
            widened = True
            print(f"widening year window to {year_from}-{year_to}", flush=True)
            continue
        if added == 0 and rounds > 3:
            break

    return results[:want], {"rounds": rounds, "widened": widened,
                            "year_from": year_from, "year_to": year_to}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    spec = _decode(a.spec)
    target = int(spec.get("target") or 30)
    seed_artists = [s for s in (spec.get("seed_artists") or []) if s]
    searches = _normalize_searches(spec.get("searches"))
    countries = [c for c in (spec.get("countries") or []) if c]
    year_from = spec.get("year_from")
    year_to = spec.get("year_to")

    derived = None
    if not searches:
        if not seed_artists:
            print("spec has neither searches nor seed_artists", file=sys.stderr)
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump({"status": "error", "error": "empty spec", "results": []}, f)
            return 1
        print(f"deriving axes from {len(seed_artists)} seed artists…", flush=True)
        derived = artist_axes.axes_for_artists(seed_artists)
        searches = _normalize_searches(derived["searches"])
        if not countries:
            countries = derived["countries"]
        # The phone's window is computed from the user's OWN records and wins;
        # the derived one only fills a gap.
        if not year_from or not year_to:
            lo, hi = derived["years"]
            if lo and hi:
                year_from, year_to = lo, hi
        print(f"derived searches: {searches}", flush=True)
        print(f"derived countries: {countries}, years: {derived['years']}", flush=True)

    year_from = int(year_from or discogs_ext.YEAR_FROM)
    year_to = int(year_to or discogs_ext.YEAR_TO)
    if year_to < year_from:
        year_from, year_to = year_to, year_from
    if year_to - year_from < MIN_WINDOW_YEARS:
        pad = (MIN_WINDOW_YEARS - (year_to - year_from) + 1) // 2
        year_from, year_to = _widen(year_from, year_to, pad)
        print(f"window padded to {year_from}-{year_to} (floor {MIN_WINDOW_YEARS}y)", flush=True)

    results, how = collect(searches, countries, year_from, year_to, target)

    out = {
        "status": "done" if results else "empty",
        "spec_used": {
            "id": spec.get("id", ""),
            "searches": searches,
            "countries": countries,
            "year_from": how["year_from"],
            "year_to": how["year_to"],
            "target": target,
            "derived": derived is not None,
            "widened": how["widened"],
            "rounds": how["rounds"],
            "coverage": (derived or {}).get("coverage", {}),
        },
        "results": results,
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {len(results)} candidates to {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
