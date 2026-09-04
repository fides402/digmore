"""Same matrix as calib2.py, but using the Space's own offset_sec (server-side
ffmpeg -ss) instead of a local ffmpeg cut, now that the HF Space has been
redeployed with the offset_sec fix (see HANDOFF.md, 04/09/2026 session).

If offset_sec is actually wired up, this should reproduce calib2.py's "dal
centro" numbers:
  perigeo <-> arti+mestieri      ~0.500
  arti+mestieri <-> weather report ~0.633 (best pair)
  cannibal corpse <-> chopin     ~0.289 (worst pair)
  ziad rahbani <-> chopin        ~0.596

Sends the SAME raw chunk (bytes=0-N, no local cut) three times per track, each
time with a different offset_sec, and lets the engine do the seeking.
"""
import itertools, json, subprocess, sys, urllib.request

ENGINE = "https://dsa222ffd-digmore-engine.hf.space/api/embed_raw"
TRACKS = {
    "perigeo (ita jazz-rock)":       "Perigeo Genealogia",
    "arti+mestieri (ita jazz-rock)": "Arti e Mestieri Gravita 9.81",
    "weather report (fusion us)":    "Weather Report Birdland",
    "ziad rahbani (lebanon)":        "Ziad Rahbani Bala Wala Chi",
    "cannibal corpse (death metal)": "Cannibal Corpse Hammer Smashed Face",
    "chopin nocturne (solo piano)":  "Chopin Nocturne op 9 no 2 piano",
}
WINDOWS = [45, 90, 135]

def stream_url(q):
    o = subprocess.run(["yt-dlp","-f","bestaudio[protocol=https]","-g",f"ytsearch1:{q}"],
                       capture_output=True, text=True, timeout=120)
    return (o.stdout.strip().splitlines() or [None])[0]

def fetch(url, nbytes=4*1024*1024):
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{nbytes}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()

def embed(data, offset_sec):
    b = "----calib3"
    parts = (
        f"--{b}\r\n"
        'Content-Disposition: form-data; name="offset_sec"\r\n\r\n'
        f"{offset_sec}\r\n"
        f"--{b}\r\n"
        'Content-Disposition: form-data; name="audio"; filename="s.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(ENGINE, data=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["embedding"]

def norm(v):
    n = sum(x*x for x in v) ** 0.5
    return [x/n for x in v] if n > 1e-9 else v

vecs = {}
for name, q in TRACKS.items():
    print(f"[{name}]", flush=True)
    try:
        u = stream_url(q)
        raw = fetch(u)
        parts = []
        for s in WINDOWS:
            try:
                parts.append(embed(raw, s))
            except Exception as e:
                print(f"  window {s}s failed: {e}")
        if not parts:
            print("  no window embedded"); continue
        avg = [sum(p[i] for p in parts)/len(parts) for i in range(len(parts[0]))]
        vecs[name] = norm(avg)
        print(f"  ok ({len(parts)} finestre)")
    except Exception as e:
        print(f"  fallito: {e}")

print("\n=== cosine matrix (server-side offset_sec, media di 3 finestre) ===")
rows = []
for a, b in itertools.combinations(vecs, 2):
    rows.append((sum(x*y for x,y in zip(vecs[a],vecs[b])), a, b))
for s, a, b in sorted(rows, reverse=True):
    print(f"{s:.3f}  {a}  <->  {b}")
