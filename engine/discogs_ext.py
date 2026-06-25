"""Discogs candidate builder with a community-rating gate.

Extends the vendored SampleHunter ``discogs_hunter`` (random lesser-known
releases, popularity cap, year window, exclude-ids freshness, Last.fm fame
filter) with:

  * macrogenre -> Discogs genre/style mapping
  * a hard year window 1969-1983 (DIGMORE requirement)
  * a community-rating filter (average + vote count) -- the one thing the
    base module did not do. Rating lives on the release detail endpoint, which
    we have to fetch anyway to read the tracklist + cover image, so the gate
    costs no extra API calls.

Tradeoff: genuinely obscure records have very few votes. By default we *keep*
releases with fewer than ``min_votes`` votes (benefit of the doubt for crate
gems) and only reject when there are enough votes AND the average is low. Set
``require_rating=True`` to demand a real rating.
"""
import random
import time

import paths_boot  # noqa: F401  -- puts engine_libs on sys.path
import discogs_hunter as dh
import lastfm

YEAR_FROM = 1969
YEAR_TO = 1983

# Countries excluded from all searches — releases from these markets are
# generally out of scope for the DIGMORE crate-digging vibe.
_EXCLUDED_COUNTRIES = frozenset({
    "India", "IN", "China", "CN", "People's Republic of China",
})

# macrogenre id -> Discogs search params
GENRE_MAP: dict[str, dict] = {
    "soul": {"genre": "Funk / Soul"},
    "jazz": {"genre": "Jazz"},
    "ost":  {"genre": "Stage & Screen", "style": "Soundtrack"},
}


def _first_label(rel: dict) -> str:
    lab = rel.get("label")
    if isinstance(lab, list):
        return lab[0] if lab else ""
    return lab or ""


def get_release_detail(release_id: int) -> dict:
    """One detail call -> tracklist + community rating + primary cover image."""
    data = dh._get(f"/releases/{release_id}")
    comm = data.get("community") or {}
    rating = comm.get("rating") or {}

    album_artists = [dh._clean(a["name"]) for a in data.get("artists", [])]
    album_artist = " & ".join(album_artists) if album_artists else ""

    tracks = []
    for t in data.get("tracklist", []):
        title = (t.get("title") or "").strip()
        if not title:
            continue
        # only real tracks (skip headings / index entries with no position)
        if (t.get("type_") or "track") != "track":
            continue
        ta = t.get("artists", [])
        artist = dh._clean(ta[0]["name"]) if ta else album_artist
        tracks.append({"artist": artist, "title": title})

    images = data.get("images") or []
    cover = ""
    for im in images:
        if im.get("type") == "primary":
            cover = im.get("uri") or im.get("resource_url") or ""
            break
    if not cover and images:
        cover = images[0].get("uri") or ""

    return {
        "tracks": tracks,
        "rating_avg": float(rating.get("average") or 0.0),
        "rating_count": int(rating.get("count") or 0),
        "cover_image": cover,
    }


def build_candidates(
    profile: str,
    max_have: int = 800,
    min_rating: float = 3.8,
    min_votes: int = 2,
    require_rating: bool = False,
    max_listeners: int = 400_000,
    n_releases: int = 18,
    tracks_per_release: int = 4,
    exclude_ids: set | None = None,
) -> tuple[list[dict], dict]:
    """Return (candidates, diag) for a macrogenre profile.

    candidates: shuffled [{artist,title,release_title,year,label,country,
    discogs_id,rating_avg,rating_count,cover_image}].
    """
    params = GENRE_MAP.get(profile, {"genre": ""})
    releases = dh.search_releases(
        genre=params.get("genre", ""),
        style=params.get("style", ""),
        year_from=YEAR_FROM,
        year_to=YEAR_TO,
        max_have=max_have,
        n=n_releases,
        exclude_ids=exclude_ids,
    )

    diag = {
        "releases_found": len(releases),
        "country_rejected": 0,
        "rating_rejected": 0,
        "tracks_total": 0,
        "filtered_famous": 0,
        "release_ids": [],
    }
    candidates: list[dict] = []

    for rel in releases:
        try:
            # country filter (post-fetch — Discogs API has no "not country" param)
            rel_country = rel.get("country", "") or ""
            if rel_country in _EXCLUDED_COUNTRIES:
                diag["country_rejected"] += 1
                continue

            det = get_release_detail(rel["id"])
            votes = det["rating_count"]
            avg = det["rating_avg"]

            # rating gate
            if require_rating and votes < min_votes:
                diag["rating_rejected"] += 1
                continue
            if votes >= min_votes and avg < min_rating:
                diag["rating_rejected"] += 1
                continue

            tracks = det["tracks"]
            if not tracks:
                continue

            diag["release_ids"].append(rel["id"])
            cover = det["cover_image"] or rel.get("cover_image", "") or rel.get("thumb", "")
            chosen = random.sample(tracks, min(tracks_per_release, len(tracks)))
            diag["tracks_total"] += len(chosen)
            for t in chosen:
                if lastfm.is_too_famous(t["artist"], max_listeners):
                    diag["filtered_famous"] += 1
                    continue
                candidates.append({
                    "artist":        t["artist"],
                    "title":         t["title"],
                    "release_title": rel.get("title", ""),
                    "year":          rel.get("year"),
                    "label":         _first_label(rel),
                    "country":       rel.get("country", ""),
                    "discogs_id":    rel["id"],
                    "rating_avg":    round(avg, 2),
                    "rating_count":  votes,
                    "cover_image":   cover,
                })
            time.sleep(0.4)   # ~60 req/min Discogs limit
        except Exception:
            continue

    random.shuffle(candidates)
    return candidates, diag
