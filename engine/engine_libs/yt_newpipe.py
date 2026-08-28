"""YouTube resolution via the newpipe-cli JVM tool, instead of yt-dlp.

yt_hunter.py's plain HTML-scrape/ytsearch8 extraction is documented (see
profiles.py) as blocked by YouTube from datacenter IPs (Hugging Face, and by
the same reasoning GitHub Actions runners) — that's exactly why profile
embeddings are pre-built locally instead of on request. newpipe-cli wraps
NewPipeExtractor (mimics YouTube's own InnerTube client, the same approach
diggaplayer/jatz use for on-device streaming) which is far more resistant to
that block. This module gives digger.py the same two-function shape as
yt_hunter (search_yt_for_track / download_snippet) so it can be swapped in
via digger.run(..., yt_module=yt_newpipe) without touching the scoring logic.

Only used by generate_cli.py (the GitHub Actions entrypoint) — the local
server (app.py) keeps using yt_hunter.py unchanged.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_CLI = _HERE.parent.parent / "newpipe-cli" / "build" / "install" / "newpipe-cli" / "bin" / "newpipe-cli"

SNIPPET_DIR = _HERE / ".cache" / "snippets_cloud"
SNIPPET_DIR.mkdir(parents=True, exist_ok=True)

_FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")

# video_id -> direct stream URL, populated by search_yt_for_track, consumed
# by download_snippet in the same process (URLs are short-lived, never cached
# to disk).
_stream_urls: dict[str, str] = {}


def _cli_path() -> str:
    p = os.environ.get("NEWPIPE_CLI_PATH", "").strip()
    if p:
        return p
    win = str(_DEFAULT_CLI) + (".bat" if os.name == "nt" else "")
    if Path(win).is_file():
        return win
    return str(_DEFAULT_CLI)


def search_yt_for_track(artist: str, title: str, use_cache: bool = True) -> dict | None:
    """Same contract as yt_hunter.search_yt_for_track: {video_id, title, channel, duration, url} or None."""
    cli = _cli_path()
    try:
        proc = subprocess.run(
            [cli, "resolve", "--artist", artist, "--title", title],
            capture_output=True, text=True, timeout=90,
        )
    except Exception as e:
        print(f"[yt_newpipe] subprocess failed for {artist} - {title}: {e}", flush=True)
        return None

    out = (proc.stdout or "").strip().splitlines()
    line = out[-1] if out else ""
    if not line:
        if proc.stderr:
            print(f"[yt_newpipe] {artist} - {title}: {proc.stderr.strip()[-300:]}", flush=True)
        return None
    try:
        data = json.loads(line)
    except Exception:
        print(f"[yt_newpipe] bad JSON for {artist} - {title}: {line[:200]}", flush=True)
        return None

    if "error" in data or "videoId" not in data:
        return None

    vid = data["videoId"]
    _stream_urls[vid] = data.get("streamUrl", "")
    return {
        "video_id": vid,
        "title":    data.get("title", ""),
        "channel":  "",
        "duration": data.get("durationSec", 0),
        "url":      f"https://www.youtube.com/watch?v={vid}",
    }


def download_snippet(video_id: str, start: int = 40, dur: int = 30) -> str:
    """Ffmpeg-pull `dur` seconds starting at `start` from the direct stream URL
    resolved during search_yt_for_track, transcoded to mp3 — same on-disk
    shape as yt_hunter.download_snippet, so clap_model.embed_audio doesn't
    need to know which resolver produced the file."""
    url = _stream_urls.get(video_id)
    if not url:
        raise RuntimeError(f"no resolved stream URL cached for {video_id} (call search_yt_for_track first)")

    target = SNIPPET_DIR / f"{video_id}.mp3"
    if target.exists() and target.stat().st_size > 0:
        return str(target)

    with tempfile.NamedTemporaryFile(suffix=".mp3", dir=SNIPPET_DIR, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    cmd = [
        _FFMPEG, "-y", "-ss", str(start), "-i", url, "-t", str(dur),
        "-vn", "-acodec", "libmp3lame", "-b:a", "128k", str(tmp_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"ffmpeg failed for {video_id}: {proc.stderr[-300:]}")

    tmp_path.replace(target)
    return str(target)
