"""Deterministic playlist sequencing and the frozen TXT renderer."""
import math
import re
import unicodedata

import numpy as np


def _cos_dist(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return float(np.clip(1 - np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9), 0, 1))


def _camelot(c):
    m = re.fullmatch(r"(1[0-2]|[1-9])([AB])", str(c or ""))
    return (int(m.group(1)), m.group(2)) if m else None


def harmonic_cost(a, b):
    ca, cb = _camelot(a.get("camelot")), _camelot(b.get("camelot"))
    conf = min(float(a.get("key_conf", 0)), float(b.get("key_conf", 0)))
    if not ca or not cb:
        return 0.35 * conf
    na, la = ca; nb, lb = cb
    ring = min((na - nb) % 12, (nb - na) % 12)
    if ca == cb: raw = 0.0
    elif na == nb and la != lb: raw = 0.10
    elif ring == 1 and la == lb: raw = 0.10
    elif ring == 2 and la == lb: raw = 0.35
    else: raw = 0.35 + 0.65 * min(ring, 6) / 6
    return raw * conf


def relation(a, b):
    ca, cb = _camelot(a.get("camelot")), _camelot(b.get("camelot"))
    if min(float(a.get("key_conf", 0)), float(b.get("key_conf", 0))) < .35 or not ca or not cb:
        return "tonalità incerta"
    sa, sb = a.get("camelot"), b.get("camelot")
    if ca == cb: return f"stessa tonalità ({sa})"
    if ca[0] == cb[0] and ca[1] != cb[1]: return f"relativa maggiore/minore ({sa}→{sb})"
    if ca[1] == cb[1] and min((ca[0]-cb[0]) % 12, (cb[0]-ca[0]) % 12) == 1:
        return f"+1 sulla ruota ({sa}→{sb})"
    return "salto di tonalità"


def _fold_bpm(v):
    v = float(v or 0)
    if v <= 0: return 0
    while v < 70: v *= 2
    while v >= 140: v /= 2
    return v


def _transition(ti, tj, fi, fj):
    bi, bj = _fold_bpm(fi.get("bpm")), _fold_bpm(fj.get("bpm"))
    tempo = 0 if not bi or not bj or abs(bi-bj) / max(bi, bj) < .06 else min(1, abs(bi-bj)/40)
    energy = max(0, abs(float(fi.get("energy", .5))-float(fj.get("energy", .5)))-.15)/.5
    z = lambda k: fi.get(k, np.zeros(1))
    w = lambda k: fj.get(k, np.zeros(1))
    cost = (.30*harmonic_cost(fi, fj) + .20*_cos_dist(z("mfcc"), w("mfcc"))
            + .15*_cos_dist(z("clap"), w("clap")) + .10*_cos_dist(z("instr"), w("instr"))
            + .10*tempo + .15*min(1, energy))
    if str(ti.get("artist", "")).casefold() == str(tj.get("artist", "")).casefold(): cost += .8
    if (ti.get("discogs_id") and ti.get("discogs_id") == tj.get("discogs_id")) or (
        ti.get("release_title") and str(ti.get("release_title")).casefold() == str(tj.get("release_title", "")).casefold()): cost += .6
    try:
        if abs(int(ti.get("year"))-int(tj.get("year"))) > 12: cost += .15
    except (ValueError, TypeError): pass
    return cost


def _target(p):
    return .45 + .40*math.sin(math.pi*min(p/.65, 1)**.9)*(1-.55*max(0,p-.65)/.35)


def sequence(tracks: list[dict], feats: dict[str, dict]) -> dict:
    analyzed = [i for i,t in enumerate(tracks) if feats.get(t.get("video_id"))]
    missing = [i for i in range(len(tracks)) if i not in analyzed]
    if not analyzed:
        return {"order": list(tracks), "transitions": [], "unanalyzed": list(tracks),
                "cost": 0.0, "sequenced": False}
    def f(i): return feats[tracks[i]["video_id"]]
    pair = {(i,j): _transition(tracks[i], tracks[j], f(i), f(j)) for i in analyzed for j in analyzed if i != j}
    ideal = max((i for i in analyzed if f(i).get("key_conf",0)>=.6 and .35<=f(i).get("energy",.5)<=.65),
                key=lambda i: tracks[i].get("clap",0), default=max(analyzed, key=lambda i: tracks[i].get("clap",0)))
    starts = sorted(analyzed, key=lambda i: (i != ideal, -float(tracks[i].get("clap",0)), i))[:8]
    def total(order):
        c = sum(pair[a,b] for a,b in zip(order, order[1:]))
        den = max(1,len(order)-1)
        c += .9*sum((float(f(i).get("energy",.5))-_target(p/den))**2 for p,i in enumerate(order))
        if order and order[0] == ideal: c -= .5
        # third identical leading instrumentation descriptor
        for k in range(2,len(order)):
            tops=[f(order[x]).get("instr_top",[""])[0] for x in (k-2,k-1,k)]
            if tops[0] and len(set(tops)) == 1: c += .35
        return c
    seeds=[]
    for start in starts:
        order=[start]; remain=set(analyzed)-{start}
        while remain:
            nxt=min(remain, key=lambda j:(pair[order[-1],j],j)); order.append(nxt); remain.remove(nxt)
        seeds.append(order)
    order=min(seeds,key=total); score=total(order); evaluations=0; stale=0
    while evaluations < 20000 and stale < 2000:
        improved=False
        for i in range(1,len(order)-1):
            for j in range(i+1,len(order)):
                candidate=order[:i]+list(reversed(order[i:j+1]))+order[j+1:]
                val=total(candidate); evaluations+=1
                if val < score-1e-9: order,score=candidate,val; improved=True; stale=0; break
                stale+=1
                if evaluations>=20000 or stale>=2000: break
            if improved or evaluations>=20000 or stale>=2000: break
        if not improved and stale < 2000:
            # deterministic or-opt, segments 1..3
            for size in (1,2,3):
                for i in range(1,max(1,len(order)-size+1)):
                    seg=order[i:i+size]; rest=order[:i]+order[i+size:]
                    for pos in range(1,len(rest)+1):
                        candidate=rest[:pos]+seg+rest[pos:]
                        val=total(candidate); evaluations+=1
                        if val < score-1e-9: order,score=candidate,val; improved=True; stale=0; break
                        stale+=1
                        if evaluations>=20000 or stale>=2000: break
                    if improved or evaluations>=20000 or stale>=2000: break
                if improved or evaluations>=20000 or stale>=2000: break
        if not improved: break
    # Insert failures away from endpoints, minimizing metadata discontinuity.
    for idx in missing:
        def fallback(pos):
            c=0.0
            for neighbor in (order[pos-1] if pos else None, order[pos] if pos<len(order) else None):
                if neighbor is None: continue
                if str(tracks[idx].get("artist","")).casefold()==str(tracks[neighbor].get("artist","")).casefold(): c+=.8
                try: c += min(.3,abs(int(tracks[idx].get("year"))-int(tracks[neighbor].get("year")))/80)
                except (ValueError,TypeError): pass
                c += abs(float(tracks[idx].get("clap",0))-float(tracks[neighbor].get("clap",0)))
            return c
        positions=range(1,len(order)) if len(order)>1 else range(1,2)
        pos=min(positions,key=lambda p:(fallback(p),p)); order.insert(pos,idx)
    transitions=[]
    for a,b in zip(order,order[1:]):
        fa,fb=(feats.get(tracks[a].get("video_id")),feats.get(tracks[b].get("video_id")))
        transitions.append({"from":a,"to":b,"relation":relation(fa,fb) if fa and fb else "tonalità incerta"})
    return {"order":[tracks[i] for i in order],"transitions":transitions,
            "unanalyzed":[tracks[i] for i in missing],"cost":round(score,6),"sequenced":True}


def _clean(value):
    s="".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in str(value or ""))
    return re.sub(r"\s+"," ",s.replace("—","-")).strip()


def _clean_title(value):
    # Discogs credits carry alt-title/translation/transliteration after " = "
    # (e.g. "My Favorite Things = マイ・フェイヴァリット・シングス"); a Spotify
    # search on the whole string never clears the importer's match threshold,
    # so keep only the first form.
    return _clean(value).split(" = ", 1)[0].strip()


def _clean_artist(value):
    # Discogs sometimes repeats the same credit on both sides of "&" (multi-role
    # entries on one release, e.g. "John Coltrane & John Coltrane") — collapse
    # duplicate parts so the search string isn't self-defeating. Genuinely
    # different collaborators are left untouched.
    parts, seen, out = [p.strip() for p in _clean(value).split(" & ")], set(), []
    for p in parts:
        key = p.casefold()
        if key and key not in seen:
            seen.add(key); out.append(p)
    return " & ".join(out) if out else _clean(value)


def render_txt(tracks):
    return "\n".join(f"{_clean_artist(t.get('artist'))} — {_clean_title(t.get('title'))}" for t in tracks) + ("\n" if tracks else "")


def slugify(name):
    s=unicodedata.normalize("NFKD",str(name or "playlist")).encode("ascii","ignore").decode().lower()
    return re.sub(r"^-|-$","",re.sub(r"[^a-z0-9]+","-",s)) or "playlist"
