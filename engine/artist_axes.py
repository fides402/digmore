"""Discogs search axes derived from a list of artist names.

DIGMORE v2 (see E:\\diggaplayer\\PIANO_DIGMORE_V2.md) profiles the user's taste
dynamically — from their Spotify liked songs, or from the contents of a link —
instead of using the three fixed macrogenres in ``discogs_ext.GENRE_MAP``. The
phone knows *which artists* the profile is made of and *what era* they come
from, but it cannot know what to ask Discogs for: Spotify's web-player GraphQL
API does not expose artist genres at all (``SpotifyArtist.genres`` comes back
empty), and ``/v1/audio-features`` is not available to a session token.

So the mapping is done here, from Discogs itself: look each seed artist up,
read the ``genre``/``style``/``country``/``year`` that Discogs already puts on
their releases, and turn the histogram into search axes. That side-steps the
whole problem of translating a Spotify tag like "italian library music" into
Discogs' own vocabulary ("Stage & Screen" / "Library Music") — there is no
translation, because both ends of the query are Discogs.

The cache is a plain JSON file committed back to the repo by the workflow:
a GitHub Actions runner has no other persistent storage (the same reason
results are committed rather than uploaded as artifacts — this account's
artifact quota is exhausted).
"""
import json
import os
import time
from pathlib import Path

import paths_boot  # noqa: F401  -- puts engine_libs on sys.path
import discogs_hunter as dh

CACHE_PATH = Path(__file__).resolve().parents[1] / "state" / "artist-axes.json"

# Discogs allows 60 authenticated requests/minute. One lookup per artist plus
# this pause keeps a 40-artist profile at roughly 45 seconds and well clear of
# the limit — a 429 here would leave half the seed artists without axes and
# produce a lopsided `searches` list with nothing to signal it went wrong.
_SLEEP = 1.1

# A style has to show up for at least this many DISTINCT seed artists to make
# it into the search axes. Discogs' search matches artist names literally, so
# an ambiguous one ("Chicago", "Air", "Bread", "Nice") pulls in a completely
# different act; a single impostor can't clear a threshold of two.
_MIN_ARTISTS_PER_STYLE = 2

# Discogs genres/styles that are not music, or not a sound. An artist who also
# wrote for radio or theatre carries them on their page, and they were being
# turned into search axes: a real dig for a Lebanese songwriter went to Discogs
# asking for "Non-Music/Radioplay" and "Jazz/Comedy" and came back with spoken
# recordings. Nothing downstream can rescue that — CLAP will happily embed a
# radio play and score it against a song.
_NON_MUSICAL_GENRES = frozenset({
    "non-music", "brass & military", "children's",
})
_NON_MUSICAL_STYLES = frozenset({
    "radioplay", "comedy", "spoken word", "interview", "monolog", "dialogue",
    "poetry", "audiobook", "education", "field recording", "sermon",
    "political", "religious", "speech", "sound art", "special effects",
    "public broadcast", "story", "fiction", "health-fitness",
})

# "Easy Listening" is a real Discogs style, not excluded for being non-music
# like the set above — excluded because as a SEARCH AXIS it has failed three
# separate live BLUESKIES runs in a row (18/09/2026, see HANDOFF.md "Cosa NON
# ha funzionato"), regardless of how the axes were derived (hand-picked, then
# seed-artist-derived). It survives derivation because genuinely sophisticated
# vocal artists (Burt Bacharach, The Singers Unlimited) carry the tag on
# Discogs, but the bucket itself is dominated by generic 1950s-70s lounge/
# mood-music orchestras (André Previn And His Orchestra, Bert Kaempfert & His
# Orchestra, Floyd Cramer, Boots Randolph, Leo Diamond, The Three Suns) that
# CLAP + the vocal/instrumental mood bonus were not discriminating against
# strongly enough. Blunt but evidence-backed: drop it as a search axis
# entirely rather than trying a fourth scoring fix on top of it.
_UNRELIABLE_SEARCH_STYLES = frozenset({"easy listening"})

_MAX_RELEASES_PER_ARTIST = 25
_MAX_SEARCHES = 8
_MAX_COUNTRIES = 3
_COUNTRY_MIN_SHARE = 0.20

# A genre must be at least this share of the seed artists' catalogue to
# contribute search axes. See the note in axes_for_artists.
_GENRE_MIN_SHARE = 0.20


def _norm(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[artist_axes] could not write cache: {e}", flush=True)


def _lookup(name: str) -> dict:
    """Genres/styles/countries/years Discogs associates with one artist.

    Uses the release search rather than /artists/{id}: it needs no id
    resolution step, and the search result rows already carry genre, style,
    country and year, so one call yields everything.
    """
    data = dh._get("/database/search", {
        "artist": name,
        "type": "release",
        "per_page": _MAX_RELEASES_PER_ARTIST,
        "page": 1,
    })
    rows = (data or {}).get("results") or []
    genres, styles, countries, years = [], [], [], []
    # (style, genre) pairs taken from the SAME release row. Attributing a
    # style to the artist's most common genre instead produces nonsense on
    # anyone who works across genres: for a soundtrack composer every style,
    # "Soul-Jazz" and "Cool Jazz" included, would end up filed under
    # "Stage & Screen", and Discogs returns almost nothing for those pairs.
    pairs: list[list[str]] = []
    for r in rows:
        row_genres = [g for g in (r.get("genre") or []) if g and g.lower() not in _NON_MUSICAL_GENRES]
        row_styles = [
            s for s in (r.get("style") or [])
            if s and s.lower() not in _NON_MUSICAL_STYLES
            and s.lower() not in _UNRELIABLE_SEARCH_STYLES
        ]
        genres += row_genres
        styles += row_styles
        for s in row_styles:
            for g in row_genres:
                pairs.append([s, g])
        c = (r.get("country") or "").strip()
        if c:
            countries.append(c)
        y = r.get("year")
        try:
            y = int(str(y)[:4])
        except (TypeError, ValueError):
            y = 0
        if 1900 < y < 2100:
            years.append(y)
    return {
        "genres": genres, "styles": styles, "pairs": pairs,
        "countries": countries, "years": years,
        "releases": len(rows),
    }


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * p))
    return ordered[max(0, min(idx, len(ordered) - 1))]


def axes_for_artists(names: list[str], use_cache: bool = True) -> dict:
    """Search axes for a set of seed artists.

    Returns ``{"searches": [{"genre","style"}], "countries": [...],
    "years": [lo, hi], "coverage": {...}}``. ``searches`` is ordered by how
    often the style appears across the seeds, so the first combos are the ones
    most representative of the profile — ``discogs_ext.build_candidates``
    samples three of them per round.
    """
    cache = _load_cache() if use_cache else {}
    fresh = 0
    per_artist: dict[str, dict] = {}

    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        key = _norm(name)
        if key in per_artist:
            continue
        hit = cache.get(key)
        if hit is None:
            try:
                hit = _lookup(name)
            except Exception as e:  # a single bad artist must not sink the run
                print(f"[artist_axes] lookup failed for {name!r}: {e}", flush=True)
                hit = {"genres": [], "styles": [], "countries": [], "years": [], "releases": 0}
            cache[key] = hit
            fresh += 1
            time.sleep(_SLEEP)
        per_artist[key] = hit

    if fresh:
        _save_cache(cache)

    # Two counts per style: how many releases mention it (ranking) and how
    # many distinct artists do (the anti-impostor gate).
    style_releases: dict[str, int] = {}
    style_artists: dict[str, set] = {}
    genre_of_style: dict[str, dict] = {}
    genre_releases: dict[str, int] = {}
    country_releases: dict[str, int] = {}
    all_years: list[int] = []
    found = 0

    for key, info in per_artist.items():
        if info.get("releases"):
            found += 1
        for g in info["genres"]:
            genre_releases[g] = genre_releases.get(g, 0) + 1
        for s in info["styles"]:
            if s.lower() in _UNRELIABLE_SEARCH_STYLES:
                continue
            style_releases[s] = style_releases.get(s, 0) + 1
            style_artists.setdefault(s, set()).add(key)
        # Which genre does this style live under? Discogs styles are not
        # globally unique across genres ("Soul-Jazz" is Jazz, "Funk" is
        # Funk / Soul) and the search API needs the pair, not the style alone.
        # The pairing has to come from the same release row (see _lookup).
        # Filtered here too, not just in _lookup: most seed artists are
        # already in the committed cache (state/artist-axes.json) from BEFORE
        # this exclusion existed, so their cached "styles"/"pairs" still
        # contain "Easy Listening" — re-deriving axes from an unchanged seed
        # list would otherwise silently ignore this fix.
        for s, g in info.get("pairs", []):
            if s.lower() in _UNRELIABLE_SEARCH_STYLES:
                continue
            bucket = genre_of_style.setdefault(s, {})
            bucket[g] = bucket.get(g, 0) + 1
        for c in info["countries"]:
            country_releases[c] = country_releases.get(c, 0) + 1
        all_years += info["years"]

    # The two-artist gate only makes sense when there ARE several artists to
    # corroborate each other. Applied to a one- or two-artist profile it
    # rejects every style and drops the search to bare genres — seen live on a
    # single-artist link, which turned into a Discogs query for
    # "Hip Hop, Electronic, Rock, Pop" and dug up unrelated catalogue. With few
    # seeds there is no impostor to filter out anyway: a wrong artist would BE
    # the profile, and no threshold saves that.
    gate = _MIN_ARTISTS_PER_STYLE if found >= 3 else 1
    ranked_styles = sorted(
        (s for s in style_releases if len(style_artists[s]) >= gate),
        key=lambda s: style_releases[s],
        reverse=True,
    )
    searches = []
    for s in ranked_styles[:_MAX_SEARCHES]:
        pairs = genre_of_style.get(s) or {}
        genre = max(pairs, key=pairs.get) if pairs else ""
        searches.append({"genre": genre, "style": s})

    # Drop the marginal genres.
    #
    # An artist's Discogs page spans everything they have ever been credited
    # on, so taking the top styles by raw frequency mixes worlds: a hip-hop
    # producer with a handful of ambient credits yielded "Hip Hop/Conscious,
    # Jazzy Hip-Hop, Electronic/Ambient, Dream Pop, Ethereal, Shoegaze, New
    # Age, Drone" — and since Discogs then samples those combos evenly, half
    # the candidate pool was guaranteed to have nothing to do with the
    # reference before a single note was heard. The CLAP ranking can only sort
    # what it is given; it cannot conjure near matches out of a pool that has
    # none, which is exactly how a dig comes back "generically the right genre
    # but musically far away".
    #
    # So a genre has to account for a real share of the artist's catalogue to
    # stay. Relaxed rather than enforced when it would leave too little to
    # search: a thin set of axes returns almost nothing, which is a worse
    # failure than a slightly wide one.
    total_genre = sum(genre_releases.values()) or 1
    shares = {g: n / total_genre for g, n in genre_releases.items()}
    strong = {g for g, sh in shares.items() if sh >= _GENRE_MIN_SHARE}
    focused = [q for q in searches if q["genre"] in strong]
    if focused:
        dropped = [q["style"] for q in searches if q not in focused]
        if dropped:
            print(
                f"[artist_axes] dropped marginal-genre styles {dropped} "
                f"(shares: { {g: round(v, 2) for g, v in shares.items()} })",
                flush=True,
            )
        # Too few axes returns almost nothing, so the set still has to be
        # widened — but widened INSIDE the dominant genre, with its bare
        # genre search, never by letting a 10%-share genre back in. Going
        # wide in the right place beats going wide in the wrong one.
        if len(focused) < 3:
            top = max(strong, key=lambda g: shares[g])
            if not any(q["genre"] == top and not q["style"] for q in focused):
                focused.append({"genre": top, "style": ""})
                print(f"[artist_axes] widened within '{top}' instead of across genres", flush=True)
        searches = focused

    # Nothing cleared the two-artist gate (a very small or very eclectic
    # profile): fall back to bare genres, which are far coarser but never
    # empty. Better a wide dig than no dig.
    if not searches:
        for g in sorted(genre_releases, key=genre_releases.get, reverse=True)[:4]:
            searches.append({"genre": g, "style": ""})

    total_country = sum(country_releases.values()) or 1
    countries = [
        c for c in sorted(country_releases, key=country_releases.get, reverse=True)
        if country_releases[c] / total_country >= _COUNTRY_MIN_SHARE
    ][:_MAX_COUNTRIES]

    years = [_percentile(all_years, 0.10), _percentile(all_years, 0.90)] if all_years else [0, 0]

    return {
        "searches": searches,
        "countries": countries,
        "years": years,
        "coverage": {
            "artists_asked": len(per_artist),
            "artists_found": found,
            "fresh_lookups": fresh,
            "styles_considered": len(style_releases),
        },
    }


if __name__ == "__main__":  # manual smoke test
    import sys
    print(json.dumps(axes_for_artists(sys.argv[1:]), ensure_ascii=False, indent=2))
