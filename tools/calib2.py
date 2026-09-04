"""Same matrix, but from the MIDDLE of each track instead of its first 30s.

The app currently embeds the opening 30 seconds of everything, because the
engine's offset_sec parameter exists in the repo but the Hugging Face Space
has not been redeployed. An opening is the least representative part of a
record: intros, silence, fade-ins, a solo instrument before the band enters.
If that is why the similarity matrix is noise, cutting a window from the
middle should reorder it — and the two Italian jazz-rock records, currently
the FURTHEST apart of any pair, should come together.

ffmpeg runs locally here, so each upload is a complete, self-contained MP3 of
the chosen window: no dependency on the Space being updated.
"""
import itertools, json, subprocess, sys, tempfile, os, urllib.request

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

def cut(raw, start):
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(raw); src = f.name
    dst = src + f".{start}.mp3"
    p = subprocess.run(["ffmpeg","-y","-ss",str(start),"-i",src,"-t","30","-vn",
                        "-acodec","libmp3lame","-b:a","128k",dst],
                       capture_output=True, text=True, timeout=60)
    os.unlink(src)
    if p.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 8000:
        return None
    d = open(dst,"rb").read(); os.unlink(dst); return d

def embed(data):
    b = "----calib"
    body = (f"--{b}\r\n"'Content-Disposition: form-data; name="audio"; filename="s.mp3"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(ENGINE, data=body,
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
        parts = [embed(c) for s in WINDOWS if (c := cut(raw, s))]
        if not parts:
            print("  no window decoded"); continue
        avg = [sum(p[i] for p in parts)/len(parts) for i in range(len(parts[0]))]
        vecs[name] = norm(avg)
        print(f"  ok ({len(parts)} finestre)")
    except Exception as e:
        print(f"  fallito: {e}")

print("\n=== cosine matrix (meta-brano, media di 3 finestre) ===")
rows = []
for a, b in itertools.combinations(vecs, 2):
    rows.append((sum(x*y for x,y in zip(vecs[a],vecs[b])), a, b))
for s, a, b in sorted(rows, reverse=True):
    print(f"{s:.3f}  {a}  <->  {b}")
