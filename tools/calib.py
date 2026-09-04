"""Is the CLAP similarity used by DIGMORE actually discriminative?

Everything downstream — the ranking, the floor, the whole premise that the 30
written tracks are the 30 closest — rests on the cosine between /api/embed_raw
vectors meaning something. A real dig produced a best-of-pool of 0.433, which
is either "nothing in that pool was close" or "0.433 is what this metric
returns for anything at all". Those two readings call for opposite fixes, so
they have to be told apart with known material rather than argued about.

Takes tracks whose relationships are known in advance, embeds them exactly the
way the app does (first ~30s, via the same endpoint), and prints the matrix.
"""
import itertools
import json
import subprocess
import sys
import urllib.request

ENGINE = "https://dsa222ffd-digmore-engine.hf.space/api/embed_raw"

TRACKS = {
    # Two Italian jazz-rock/prog records: should be the closest pair here.
    "perigeo (ita jazz-rock)":       "Perigeo Genealogia",
    "arti+mestieri (ita jazz-rock)": "Arti e Mestieri Gravita 9.81",
    # Same broad world, different country/decade.
    "weather report (fusion us)":    "Weather Report Birdland",
    # The actual reference from the failing run.
    "ziad rahbani (lebanon)":        "Ziad Rahbani Bala Wala Chi",
    # Deliberately far away.
    "cannibal corpse (death metal)": "Cannibal Corpse Hammer Smashed Face",
    "chopin nocturne (solo piano)":  "Chopin Nocturne op 9 no 2 piano",
}


def stream_url(watch: str) -> str | None:
    try:
        out = subprocess.run(
            ["yt-dlp", "-f", "bestaudio[protocol=https]", "-g", f"ytsearch1:{watch}"],
            capture_output=True, text=True, timeout=90,
        )
        return (out.stdout.strip().splitlines() or [None])[0]
    except Exception as e:
        print(f"  yt-dlp failed: {e}")
        return None


def snippet(url: str, nbytes: int = 1024 * 1024) -> bytes | None:
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{nbytes}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except Exception as e:
        print(f"  range fetch failed: {e}")
        return None


def embed(data: bytes) -> list[float] | None:
    boundary = "----digmorecalib"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="s.webm"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        ENGINE, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())["embedding"]
    except Exception as e:
        print(f"  embed failed: {e}")
        return None


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def main():
    vecs = {}
    for name, watch in TRACKS.items():
        print(f"[{name}]")
        u = stream_url(watch)
        if not u:
            continue
        d = snippet(u)
        if not d:
            continue
        v = embed(d)
        if v:
            vecs[name] = v
            print(f"  ok ({len(v)} dim)")

    names = list(vecs)
    print("\n=== cosine matrix ===")
    for a, b in itertools.combinations(names, 2):
        print(f"{dot(vecs[a], vecs[b]):.3f}  {a}  <->  {b}")
    print("\n=== self-similarity sanity (should be 1.000) ===")
    for n in names[:1]:
        print(f"{dot(vecs[n], vecs[n]):.3f}  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
