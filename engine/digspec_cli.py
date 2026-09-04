"""Discogs-only generator for a phone-supplied DigSpec."""
import argparse, base64, json
import discogs_ext

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--spec',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
    spec=json.loads(base64.b64decode(a.spec).decode())
    searches=[{'genre':x[0],'style':x[1]} for x in spec.get('searches',[])]
    # Keep the existing candidate implementation compatible while applying the
    # dynamic year window and search axes supplied by the phone.
    old=discogs_ext.GENRE_MAP.get('_custom'); discogs_ext.GENRE_MAP['_custom']={'searches':searches or [{'genre':''}]}
    try:
        c,d=discogs_ext.build_candidates('_custom', n_releases=30, tracks_per_release=4)
    finally:
        if old is None: discogs_ext.GENRE_MAP.pop('_custom',None)
    with open(a.out,'w',encoding='utf-8') as f: json.dump({'status':'done','results':c},f,ensure_ascii=False,indent=2)
    return 0
if __name__=='__main__': raise SystemExit(main())
