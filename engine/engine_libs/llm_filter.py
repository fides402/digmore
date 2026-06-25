"""LLM-based content classifier via OpenRouter (free models).

Used to skip video game OST playlists when searching in OST genre mode,
where the goal is film scores and library music, not gaming soundtracks.

Two-layer approach:
  1. Fast keyword regex (catches obvious gaming titles instantly, no API call).
  2. LLM via OpenRouter for ambiguous titles the keyword misses
     (e.g. "Epic Adventure Mix" could be a game or a film).

Classifications are cached (90-day TTL) so the same title is never
classified twice.
"""
import re
import time
import requests
import kvcache

_API_KEY  = "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"
_BASE     = "https://openrouter.ai/api/v1/chat/completions"
_MODEL    = "meta-llama/llama-3.2-3b-instruct:free"
_NS       = "llm_filter"
_TTL_DAYS = 90

_HEADERS = {
    "Authorization": f"Bearer {_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "http://localhost:8077",
}

# Same patterns as yt_hunter._GAME_RE — duplicated here so llm_filter is self-contained.
_GAME_KW = re.compile(
    r"\b("
    r"video\s*game|game\s*ost|game\s*music|game\s*soundtrack|gaming\s*(music|ost|soundtrack)|"
    r"nintendo|playstation|xbox(\s*(one|360|series))?|"
    r"zelda|mario(\s*(kart|galaxy|odyssey|64))?|pok[eé]mon|minecraft|sonic|"
    r"halo|final\s*fantasy|skyrim|elder\s*scrolls|dark\s*souls|elden\s*ring|"
    r"fortnite|roblox|valorant|overwatch|league\s*of\s*legends|"
    r"genshin(\s*impact)?|persona\s*\d|kingdom\s*hearts|"
    r"chrono\s*trigger|undertale|hollow\s*knight|celeste|"
    r"metal\s*gear|resident\s*evil|silent\s*hill|"
    r"rpg\s*(game\s*)?music|jrpg|game\s*(boss|battle|theme|bgm|bgmusic)"
    r")\b",
    re.IGNORECASE,
)


def is_game_ost(title: str) -> bool:
    """Return True if title is a video game OST — should be skipped in OST mode.

    Keyword fast-path first (no network call); LLM only for ambiguous titles.
    Result cached so the same title is never classified twice.
    Conservative on errors: returns False (keep the content).
    """
    title = title.strip()
    if not title:
        return False

    # Fast path: obvious gaming keyword → skip LLM entirely
    if _GAME_KW.search(title):
        return True

    cache_key = f"game_ost::{title.lower()[:120]}"
    cached = kvcache.get(_NS, cache_key, ttl_days=_TTL_DAYS)
    if cached is not None:
        return bool(cached)

    result = _llm_classify(title)
    kvcache.put(_NS, cache_key, result)
    return result


def _llm_classify(title: str) -> bool:
    """Ask LLM: is this a video game OST? Retries once on 429."""
    prompt = (
        "Is the following a video game soundtrack, video game OST, or gaming music playlist? "
        "Do NOT count film scores, movie soundtracks, TV scores, or library music as video games. "
        "Answer with only YES or NO.\n"
        f'Title: "{title}"'
    )
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 5,
        "temperature": 0,
    }
    for attempt in range(2):
        try:
            r = requests.post(_BASE, headers=_HEADERS, json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(4 + attempt * 4)   # back off, then retry once
                continue
            r.raise_for_status()
            answer = r.json()["choices"][0]["message"]["content"].strip().upper()
            return answer.startswith("YES")
        except Exception as exc:
            print(f"[llm_filter] attempt {attempt+1} error for '{title[:60]}': {exc}")
            if attempt == 0:
                time.sleep(3)
    return False   # conservative: keep content on persistent error
