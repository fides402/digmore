/* DIGMORE — frontend v4 */
(() => {
  const APP_VERSION = '44';   // shown in the UI so we can confirm the phone is current
  const _DEFAULT_ENGINE = '';
  const _ENGINE_RESET = 'tunnel-2026-06-25b';
  if (localStorage.getItem('digmore_engine_reset') !== _ENGINE_RESET) {
    localStorage.removeItem('digmore_engine');
    localStorage.setItem('digmore_engine_reset', _ENGINE_RESET);
  }
  const ENGINE = (window.DIGMORE_ENGINE ||
    localStorage.getItem('digmore_engine') || _DEFAULT_ENGINE).replace(/\/$/, '');
  const api = p => ENGINE + p;

  const $ = id => document.getElementById(id);

  const S = {
    profile: null,
    profiles: [],
    jobId: null,
    poll: null,
    tracks: [],
    current: -1,
    playing: false,
    repeat: 'none',   // 'none' | 'one' | 'all'
    offline: false,
  };

  const LABELS = { soul: 'Chill Soul', jazz: 'Relax Jazz', ost: 'Relax OST', brazil: 'Brazil' };

  /* ── API ──────────────────────────────────────────────── */
  async function loadProfiles() {
    try {
      const r = await fetch(api('/api/profiles'));
      S.profiles = (await r.json()).profiles || [];
      S.offline = false;
    } catch {
      S.offline = true;
      setStatus('Offline — puoi sfogliare la libreria salvata.');
      return;
    }
    renderProfiles();
  }

  /* ── PROFILES ─────────────────────────────────────────── */
  function renderProfiles() {
    const el = $('profile-row');
    el.innerHTML = '';
    S.profiles.forEach(p => {
      const b = document.createElement('button');
      b.className = 'pcap' +
        (p.id === S.profile ? ' on' : '') +
        (p.state === 'no_csv' ? ' nocsv' : '');
      const stateNote = p.state === 'no_csv' ? ' · manca CSV'
        : p.state === 'not_built' ? ' · da costruire' : '';
      b.innerHTML = `<span class="pip"></span>${p.label}${stateNote}`;
      b.onclick = () => selectProfile(p);
      el.appendChild(b);
    });
  }

  function selectProfile(p) {
    if (p.state === 'no_csv') { setStatus(`Metti il CSV in profiles_csv/ per "${p.label}".`); return; }
    if (p.state === 'not_built') { triggerBuild(p); return; }
    S.profile = p.id;
    renderProfiles();
    setHeroIdle(p);
    $('gen').disabled = false;
    setStatus(`Profilo pronto · ${p.n_tracks || 0} tracce reference.`);
  }

  function setHeroIdle(p) {
    $('hero-circle').classList.remove('hidden');
    $('hero-cover').classList.remove('visible');
    $('profile-headline').innerHTML = p.label;
    $('now-title').style.display = 'none';
    $('now-artist').style.display = 'none';
    setTags([{ text: '1969–1983' }, { text: p.label }]);
  }

  async function triggerBuild(p) {
    setStatus(`Costruisco "${p.label}" — analisi CLAP delle reference…`);
    const r = await fetch(api(`/api/profiles/${p.id}/build`), { method: 'POST' });
    const d = await r.json();
    if (!d.ok && !d.job_id) { setStatus('Errore avvio build.'); return; }
    const jid = d.job_id;
    const t = setInterval(async () => {
      try {
        const s = await (await fetch(api(`/api/profiles/build-status/${jid}`))).json();
        setProgress(s.progress || 0);
        setStatus(s.msg || '');
        if (s.status === 'done' || s.status === 'error') {
          clearInterval(t); setProgress(0);
          await loadProfiles();
          if (s.status === 'done') {
            const fresh = S.profiles.find(x => x.id === p.id);
            if (fresh) selectProfile(fresh);
          }
        }
      } catch {}
    }, 1500);
  }

  /* ── GENERATION ───────────────────────────────────────── */
  async function generate() {
    if (!S.profile) return;
    $('gen').disabled = true;
    $('stop-btn').style.display = 'block';
    S.tracks = []; renderList(); fetch(api('/api/audio/cache/clear'), { method: 'POST' }).catch(() => {});
    setStatus('Avvio generazione…'); setProgress(0);
    const fd = new FormData();
    fd.append('profile', S.profile);
    fd.append('vibe_gate', 0);
    try {
      const r = await fetch(api('/api/generate'), { method: 'POST', body: fd });
      const d = await r.json();
      S.jobId = d.job_id;
      S.poll = setInterval(pollJob, 1500);
    } catch (e) {
      setStatus('Errore: ' + e.message);
      $('gen').disabled = false;
      $('stop-btn').style.display = 'none';
    }
  }

  async function pollJob() {
    if (!S.jobId) return;
    try {
      const j = await (await fetch(api(`/api/generate/${S.jobId}`))).json();
      setProgress(j.progress || 0);
      setStatus((j.status_msg || '') + (j.accepted ? `  ·  ${j.accepted}/30 brani` : ''));
      if (Array.isArray(j.results) && j.results.length) {
        const sig = j.results.map(r => r.video_id + ':' + r.vibe).join('|');
        if (sig !== S._sig) { S._sig = sig; S.tracks = j.results; renderList(); bestofUpdate(j.results); preloadBatch(j.results, 0, 5); }
      }
      if (j.status === 'done' || j.status === 'stopped' || j.status === 'error') {
        clearInterval(S.poll); S.poll = null;
        $('gen').disabled = false;
        $('stop-btn').style.display = 'none';
        setProgress(100);
        if (j.status === 'error') setStatus('Errore: ' + (j.error || '?'));
        else {
          let msg = `${S.tracks.length} brani trovati.`;
          const ti = j.taste;
          if (ti && ti.active) {
            if (ti.ready) {
              let tm = `🎧 affinato (${ti.deviation_deg}° · ${ti.positives} ascolti`;
              if (ti.neg_active) tm += `, negativi ${Math.round((ti.neg_confidence||0)*100)}%conf`;
              if (ti.reproba) tm += `, ${ti.reproba} re-proba`;
              tm += ')';
              msg += '  ·  ' + tm;
            } else msg += `  ·  🎧 in apprendimento (${ti.positives || 0}/${ti.min_pos || 6})`;
          }
          setStatus(msg);
        }
      }
    } catch {}
  }

  async function stopGen() {
    if (S.jobId) await fetch(api(`/api/generate/${S.jobId}/stop`), { method: 'POST' }).catch(() => {});
  }

  /* ── LIST ─────────────────────────────────────────────── */
  function coverSrc(t) {
    // In offline mode (or if backend unreachable), skip the proxy and use YT thumbnail
    if (S.offline || !ENGINE) {
      return t._yt_cover || (t.video_id ? `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg` : '');
    }
    if (t.cover_image) return api('/api/cover?u=' + encodeURIComponent(t.cover_image));
    if (t.video_id) return `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg`;
    return '';
  }

  function preloadBatch(tracks, from, count) {
    tracks.slice(from, from + count).forEach(t => {
      if (t && t.video_id) fetch(api('/api/preload/' + t.video_id), { method: 'POST' }).catch(() => {});
    });
  }

  function renderList() {
    const el = $('list');
    el.innerHTML = '';
    S.tracks.forEach((t, i) => {
      const row = document.createElement('div');
      row.className = 'track-row' + (i === S.current ? ' playing' : '');
      row.onclick = () => play(i);

      const idx = document.createElement('div');
      idx.className = 'tidx'; idx.textContent = i + 1;

      const img = new Image(); img.className = 'tcover';
      img.src = coverSrc(t);
      img.onerror = () => { if (t.video_id) img.src = `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg`; };

      const meta = document.createElement('div'); meta.className = 'tmeta';
      meta.innerHTML =
        `<div class="ttitle">${esc(t.title)}</div>` +
        `<div class="tartist">${esc(t.artist || '—')}</div>`;

      const right = document.createElement('div'); right.className = 'tright';
      right.innerHTML =
        `<div class="tvibe">${t.vibe || 0}%</div>` +
        `<div class="tyear">${t.year || ''}</div>`;

      if (S._isBestof) {
        const del = document.createElement('button');
        del.className = 'bo-del';
        del.title = 'Rimuovi dal best of';
        del.innerHTML = '✕';
        del.onclick = e => { e.stopPropagation(); bestofRemove(t.video_id); };
        right.appendChild(del);
      }

      row.append(idx, img, meta, right);
      el.appendChild(row);
    });

    // show/hide save button
    const saveRow = $('save-row');
    if (S.tracks.length > 0) saveRow.classList.add('on');
    else saveRow.classList.remove('on');
    // reset saved state
    saveBtnLabel('Salva playlist');
    $('btn-save').classList.remove('saved');
  }

  // Update the save button's text label without wiping its SVG icon.
  function saveBtnLabel(txt) {
    const span = $('btn-save').querySelector('span');
    if (span) span.textContent = txt;
  }

  /* ── PLAYER: YouTube IFrame (istantaneo, usa sessione browser) ── */
  class YTAudio extends EventTarget {
    constructor() {
      super();
      this._ytId = null; this._pendingSrc = null; this._player = null;
      this._ready = false; this._paused = true;
      this._curTime = 0; this._duration = 0; this._vol = 1; this._timerId = null;
      const setup = () => {
        this._player = new YT.Player('_yt_audio_player', {
          height: '1', width: '1',
          playerVars: { autoplay: 0, controls: 0, rel: 0, fs: 0, disablekb: 1, playsinline: 1 },
          events: {
            onReady: ev => { this._ready = true; ev.target.setVolume(Math.round(this._vol * 100)); if (this._pendingSrc) this._load(this._pendingSrc); },
            onStateChange: ev => this._onState(ev.data),
            onError: ev => { console.warn('[YT] error', ev.data); this.dispatchEvent(new Event('error')); },
          },
        });
      };
      if (window.YT?.Player) setup(); else window._ytAudioInit = setup;
    }
    _onState(s) {
      const PS = window.YT?.PlayerState; if (!PS) return;
      if (s === PS.PLAYING) { this._paused = false; this._startTimer(); this.dispatchEvent(new Event('play')); }
      else if (s === PS.PAUSED) { this._paused = true; this._stopTimer(); this.dispatchEvent(new Event('pause')); }
      else if (s === PS.ENDED) { this._paused = true; this._stopTimer(); this.dispatchEvent(new Event('ended')); }
    }
    _startTimer() {
      if (this._timerId) return;
      this._timerId = setInterval(() => {
        if (!this._player) return;
        try { this._curTime = this._player.getCurrentTime() || 0; this._duration = this._player.getDuration() || 0; } catch (_) { return; }
        this.dispatchEvent(new Event('timeupdate'));
      }, 500);
    }
    _stopTimer() { if (this._timerId) { clearInterval(this._timerId); this._timerId = null; } }
    set src(q) { if (!q) return; this._pendingSrc = q; if (this._ready && this._player) this._load(q); }
    get src() { return this._pendingSrc || ''; }
    _load(q) { this._ytId = q; this._player.loadVideoById(q); }
    play() { if (this._ready && this._player) this._player.playVideo(); return Promise.resolve(); }
    pause() { if (this._ready && this._player) this._player.pauseVideo(); }
    get paused() { return this._paused; }
    get currentTime() { return this._curTime; }
    set currentTime(t) { this._curTime = t; if (this._ready && this._player) this._player.seekTo(t, true); }
    get duration() { return this._duration; }
    set volume(v) { this._vol = v; if (this._ready && this._player) this._player.setVolume(Math.round(v * 100)); }
    get volume() { return this._vol; }
  }
  const audio = new YTAudio();

  /* ── Native audio fallback (per track IFrame error 101/150) ── */
  const _native = document.createElement('audio');
  _native.preload = 'none';
  _native.style.display = 'none';
  document.body.appendChild(_native);
  let _usingNative = false;

  function _activeAudio() { return _usingNative ? _native : audio; }
  // Pulisce <audio> senza far scattare un 'error' spurio (src='' risolve all'URL pagina)
  function _clearNative() { _native.pause(); _native.removeAttribute('src'); _native.load(); }

  _native.addEventListener('play',  () => { S.playing = true;  setPlayerBarPlayState(true);  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing'; });
  _native.addEventListener('pause', () => { S.playing = false; setPlayerBarPlayState(false); if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused'; });
  _native.addEventListener('ended', () => {
    _usingNative = false; _clearNative();
    flushListen();
    if (S.repeat === 'one') { _native.currentTime = 0; _native.play().catch(() => {}); }
    else if (S.repeat === 'all') play((S.current + 1) % Math.max(S.tracks.length, 1));
    else play(S.current + 1);
  });
  _native.addEventListener('timeupdate', () => {
    const dur = _native.duration || 1, cur = _native.currentTime || 0;
    if (_lt) { if (_native.duration) _lt.dur = _native.duration; _lt.maxSec = Math.max(_lt.maxSec || 0, cur); }
    $('pb-prog-inner').style.width = (cur / dur * 100) + '%';
    if ('mediaSession' in navigator && navigator.mediaSession.setPositionState) {
      try { navigator.mediaSession.setPositionState({ duration: dur, position: Math.min(cur, dur), playbackRate: 1 }); } catch {}
    }
  });
  _native.addEventListener('error', () => {
    // Proxy fallito → fallback all'IFrame YT (potrebbe essere embeddable)
    _usingNative = false; _clearNative();
    const cur = S.tracks[S.current];
    if (!cur || !cur.video_id) { play(S.current + 1); return; }
    setStatus(`${cur.title} · player YT…`);
    audio.src = cur.video_id;
    audio.play().catch(() => {});
  });

  /* ── Taste logging: solo brani effettivamente riprodotti ── */
  let _lt = null;  // { video_id, dur, maxSec }
  function flushListen() {
    const lt = _lt; _lt = null;
    if (!lt || !lt.video_id || !lt.dur) return;
    fetch('/api/listen', {
      method: 'POST', keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ video_id: lt.video_id, listened_sec: lt.maxSec || 0, duration_sec: lt.dur, profile: S.profile || '' }),
    }).catch(() => {});
  }
  window.addEventListener('pagehide', flushListen);

  audio.addEventListener('ended', () => {
    flushListen();
    if (S.repeat === 'one') { audio.currentTime = 0; audio.play().catch(() => {}); }
    else if (S.repeat === 'all') play((S.current + 1) % Math.max(S.tracks.length, 1));
    else play(S.current + 1);
  });

  audio.addEventListener('play', () => {
    S.playing = true; setPlayerBarPlayState(true);
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
  });

  audio.addEventListener('pause', () => {
    S.playing = false; setPlayerBarPlayState(false);
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
  });

  audio.addEventListener('error', () => {
    // IFrame fallito DOPO che anche il proxy ha fallito → skip (ultima istanza)
    const cur = S.tracks[S.current];
    setStatus(`Saltato "${cur ? cur.title : '?'}" — non disponibile`);
    S._errSkips = (S._errSkips || 0) + 1;
    if (S._errSkips > S.tracks.length) { S._errSkips = 0; return; }
    play(S.current + 1);
  });

  audio.addEventListener('timeupdate', () => {
    const dur = audio.duration || 1;
    const cur = audio.currentTime || 0;
    if (_lt) { if (audio.duration) _lt.dur = audio.duration; _lt.maxSec = Math.max(_lt.maxSec, cur); }
    $('pb-prog-inner').style.width = (cur / dur * 100) + '%';
    if ('mediaSession' in navigator && navigator.mediaSession.setPositionState) {
      try { navigator.mediaSession.setPositionState({ duration: dur, position: Math.min(cur, dur), playbackRate: 1 }); } catch {}
    }
  });


  function highlightCurrent() {
    const rows = $('list').querySelectorAll('.track-row');
    rows.forEach((r, idx) => r.classList.toggle('playing', idx === S.current));
  }

  function play(i) {
    if (i < 0 || i >= S.tracks.length) return;
    flushListen();   // registra l'ascolto del brano precedente
    // reset native fallback
    if (_usingNative) { _usingNative = false; _clearNative(); }
    // cancella audio precedente dal disco (teniamo solo il brano corrente)
    const prevTrack = S.tracks[S.current];
    if (prevTrack && prevTrack.video_id && S.current !== i) {
      fetch(api('/api/audio/' + prevTrack.video_id + '/delete'), { method: 'POST' }).catch(() => {});
    }
    S.current = i;
    S._errSkips = S._errSkips || 0;
    highlightCurrent();
    const t = S.tracks[i];
    _lt = { video_id: t.video_id, dur: 0, maxSec: 0 };

    const cover = $('hero-cover');
    const src = coverSrc(t);
    if (src) {
      cover.src = src;
      cover.onerror = () => { if (t.video_id) cover.src = `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg`; };
      cover.classList.add('visible');
      $('hero-circle').classList.add('hidden');
    }
    $('profile-headline').style.display = 'none';
    const nt = $('now-title'); nt.textContent = t.title; nt.style.display = 'block';
    const na = $('now-artist');
    na.textContent = (t.artist || '—') + (t.year ? ' · ' + t.year : '') + (t.label ? ' · ' + t.label : '');
    na.style.display = 'block';
    setTags([
      t.rating_avg ? { text: '★ ' + t.rating_avg } : null,
      { text: (t.vibe || 0) + '% vibe', hot: true },
      t.country ? { text: t.country } : null,
    ].filter(Boolean));

    updatePlayerBar(t);
    updateMediaSession(t);

    // Player primario: proxy audio diretto (nessun limite di embedding → no skip)
    try { audio.pause(); } catch {}
    S._errSkips = 0;
    _usingNative = true;
    _native.volume = audio.volume;
    _native.src = api('/api/stream/' + t.video_id);
    _native.play().catch(() => setStatus('Tocca play per avviare.'));

    // pre-resolve the next track URL in background so it's instant when needed
    preloadBatch(S.tracks, i + 1, 3);

    markListened(t);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function togglePlay() {
    if (S.current < 0) { play(0); return; }
    const a = _activeAudio();
    S.playing ? a.pause() : a.play().catch(() => {});
  }

  /* ── PLAYER BAR ───────────────────────────────────────── */
  function updatePlayerBar(t) {
    const bar = $('player-bar');
    if (!t) { bar.classList.remove('on'); return; }
    bar.classList.add('on');
    const src = coverSrc(t);
    $('pb-cover').src = src;
    $('pb-cover').onerror = () => { if (t.video_id) $('pb-cover').src = `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg`; };
    $('pb-title').textContent = t.title || '';
    $('pb-artist').textContent = t.artist || '';
  }

  const ICON_PLAY = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5a1 1 0 0 1 1.54-.84l10 6.5a1 1 0 0 1 0 1.68l-10 6.5A1 1 0 0 1 8 18.5v-13z"/></svg>';
  const ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
  const ICON_REPEAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>';
  const ICON_REPEAT_ONE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/><text x="12" y="15.5" font-size="9" fill="currentColor" stroke="none" text-anchor="middle" font-family="sans-serif" font-weight="700">1</text></svg>';

  function setPlayerBarPlayState(playing) {
    const btn = $('pb-play');
    if (btn) btn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  }

  $('pb-progress').addEventListener('click', e => {
    const a = _activeAudio();
    if (!a.duration) return;
    a.currentTime = (e.offsetX / e.currentTarget.offsetWidth) * a.duration;
  });

  $('pb-volume').addEventListener('input', e => {
    audio.volume = e.target.value / 100;
    _native.volume = e.target.value / 100;
  });

  function execCommandCopy(text) {
    // Synchronous fallback — works over plain http (phone) and when the async
    // Clipboard API is blocked. Must run inside the click gesture.
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
      ta.setAttribute('readonly', '');
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ta.setSelectionRange(0, text.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (e) { return false; }
  }

  $('pb-copy').addEventListener('click', () => {
    // Read straight from the player bar so we always copy whatever is shown as
    // "now playing" — immune to any S.current desync after skips/auto-advance.
    const artist = ($('pb-artist').textContent || '').trim();
    const title = ($('pb-title').textContent || '').trim();
    if (!title && !artist) return;
    const text = [artist, title].filter(Boolean).join(' – ');
    const btn = $('pb-copy');
    const clipSvg = btn.innerHTML;
    const showOk = () => {
      btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="2,8 6,12 14,4"/></svg>`;
      btn.classList.add('copied');
      setTimeout(() => { btn.innerHTML = clipSvg; btn.classList.remove('copied'); }, 1800);
    };

    // Always run the synchronous execCommand copy first — it works everywhere
    // (http LAN included) as long as it's inside this click gesture.
    const ok = execCommandCopy(text);
    if (ok) { showOk(); return; }

    // execCommand failed → try the async Clipboard API (secure contexts only)
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(showOk).catch(() => prompt('Copia manualmente:', text));
    } else {
      prompt('Copia manualmente:', text);
    }
  });

  $('pb-prev').addEventListener('click', () => { S._lastDir = -1; play(S.current - 1); });
  $('pb-next').addEventListener('click', () => { S._lastDir = 1; play(S.current + 1); });
  $('pb-play').addEventListener('click', togglePlay);
  $('pb-repeat').addEventListener('click', () => {
    S.repeat = S.repeat === 'none' ? 'all' : S.repeat === 'all' ? 'one' : 'none';
    const btn = $('pb-repeat');
    btn.innerHTML = S.repeat === 'one' ? ICON_REPEAT_ONE : ICON_REPEAT;
    btn.classList.toggle('active', S.repeat !== 'none');
    btn.title = S.repeat === 'one' ? 'Ripeti brano' : S.repeat === 'all' ? 'Ripeti playlist' : 'Ripeti';
  });

  function updateMediaSession(t) {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title || '',
      artist: t.artist || '',
      album: t.release_title || t.year || '',
      artwork: [{ src: coverSrc(t), sizes: '512x512', type: 'image/jpeg' }],
    });
    navigator.mediaSession.setActionHandler('play', () => _activeAudio().play().catch(() => {}));
    navigator.mediaSession.setActionHandler('pause', () => _activeAudio().pause());
    navigator.mediaSession.setActionHandler('nexttrack', () => play(S.current + 1));
    navigator.mediaSession.setActionHandler('previoustrack', () => play(S.current - 1));
    navigator.mediaSession.setActionHandler('seekto', e => { _activeAudio().currentTime = e.seekTime; });
  }

  async function markListened(t) {
    if (S.offline) return;
    const fd = new FormData();
    fd.append('artist', t.artist || '');
    fd.append('title', t.title || '');
    fd.append('video_id', t.video_id || '');
    try { await fetch(api('/api/listened/mark'), { method: 'POST', body: fd }); } catch {}
  }

  /* ── BEST OF THE MONTH ────────────────────────────────── */
  const BESTOF_MAX = 30;
  const BESTOF_POOL = 60;
  const _boKey = () => {
    const d = new Date();
    return `digmore_bestof_${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
  };
  const _boLabel = () => {
    const d = new Date();
    return `Best of ${d.toLocaleDateString('it-IT', { month: 'long', year: 'numeric' })}`;
  };

  function bestofLoad() {
    try { return JSON.parse(localStorage.getItem(_boKey()) || 'null'); } catch { return null; }
  }
  function bestofSave(entry) { localStorage.setItem(_boKey(), JSON.stringify(entry)); }

  function bestofUpdate(newTracks) {
    if (!newTracks || !newTracks.length) return;
    const entry = bestofLoad() || { pool: [], excluded: [], tracks: [], updatedAt: null };
    if (!entry.pool) entry.pool = entry.tracks || [];
    if (!entry.excluded) entry.excluded = [];
    const seen = new Map(entry.pool.map(t => [t.video_id, t]));
    for (const t of newTracks) {
      if (t.video_id && !seen.has(t.video_id)) {
        seen.set(t.video_id, { ...t, _yt_cover: `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg` });
      }
    }
    const excSet = new Set(entry.excluded);
    entry.pool = [...seen.values()].sort((a, b) => (b.vibe || 0) - (a.vibe || 0)).slice(0, BESTOF_POOL);
    entry.tracks = entry.pool.filter(t => !excSet.has(t.video_id)).slice(0, BESTOF_MAX);
    entry.updatedAt = new Date().toISOString();
    bestofSave(entry);
    _bestofSyncServer(entry);
  }

  function bestofRemove(video_id) {
    const entry = bestofLoad();
    if (!entry) return;
    if (!entry.pool) entry.pool = entry.tracks || [];
    if (!entry.excluded) entry.excluded = [];
    // merge current S.tracks into pool as bench candidates (covers old entries without pool)
    const poolIds = new Set(entry.pool.map(t => t.video_id));
    for (const t of (S.tracks || [])) {
      if (t.video_id && !poolIds.has(t.video_id)) {
        entry.pool.push({ ...t, _yt_cover: `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg` });
        poolIds.add(t.video_id);
      }
    }
    entry.pool.sort((a, b) => (b.vibe || 0) - (a.vibe || 0));
    entry.pool = entry.pool.slice(0, BESTOF_POOL);
    if (!entry.excluded.includes(video_id)) entry.excluded.push(video_id);
    const excSet = new Set(entry.excluded);
    entry.tracks = entry.pool.filter(t => !excSet.has(t.video_id)).slice(0, BESTOF_MAX);
    entry.updatedAt = new Date().toISOString();
    bestofSave(entry);
    _bestofSyncServer(entry);
    if (S._isBestof) { S.tracks = entry.tracks; renderList(); }
  }

  function _bestofSyncServer(entry) {
    const pl = {
      id: '__bestof_' + _boKey(),
      name: _boLabel(),
      profile: '__bestof__',
      savedAt: entry.updatedAt,
      tracks: entry.tracks,
    };
    fetch('/api/library', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pl),
    }).catch(() => {});
  }

  function openBestof() {
    const bo = bestofLoad();
    if (!bo || !bo.tracks.length) return;
    S.tracks = bo.tracks;
    S.offline = true;
    S._isBestof = true;
    fetch(api('/api/audio/cache/clear'), { method: 'POST' }).catch(() => {});
    preloadBatch(bo.tracks, 0, 5);
    renderList();
    setStatus(_boLabel() + ' · ' + bo.tracks.length + ' brani');
    closeOvEl($('ov-library'));
    $('hero-circle').classList.remove('hidden');
    $('hero-cover').classList.remove('visible');
    $('profile-headline').style.display = '';
    $('profile-headline').innerHTML = '⭐ ' + esc(_boLabel());
    $('now-title').style.display = 'none';
    $('now-artist').style.display = 'none';
    setTags([{ text: bo.tracks.length + ' brani' }, { text: 'aggiornato ' + new Date(bo.updatedAt).toLocaleDateString('it-IT') }]);
    // prime save button with snapshot label
    saveBtnLabel('Snapshot best of');
    $('btn-save').classList.remove('saved');
    // pre-fill the save dialog name
    $('save-name').value = _boLabel() + ' · ' + new Date().toLocaleDateString('it-IT', { day:'2-digit', month:'short' });
  }

  /* ── LIBRARY ──────────────────────────────────────────── */
  const _LIB_LS = 'digmore_library';

  function libLoad() {
    try { return JSON.parse(localStorage.getItem(_LIB_LS) || '[]'); } catch { return []; }
  }
  function _libCache(lib) { localStorage.setItem(_LIB_LS, JSON.stringify(lib)); }

  let _libTs = -1;
  let _libPollTimer = null;

  async function libSync(force = false) {
    if (S.offline) return null;
    try {
      const r = await fetch('/api/library');   // always relative — no tunnel dependency
      const d = await r.json();

      // Restore bestof from server if missing locally
      const serverBo = (d.playlists || []).find(p => p.id === '__bestof_' + _boKey());
      if (serverBo && !bestofLoad()) {
        bestofSave({ tracks: serverBo.tracks, updatedAt: serverBo.savedAt });
      }

      const realServer = (d.playlists || []).filter(p => !p.id.startsWith('__bestof_'));
      const localLib   = libLoad().filter(p => !p.id.startsWith('__bestof_'));

      // Push any local-only playlists to server
      const serverIds = new Set(realServer.map(p => p.id));
      for (const pl of localLib.filter(p => !serverIds.has(p.id))) {
        fetch('/api/library', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(pl),
        }).catch(() => {});
        realServer.push(pl);
      }
      realServer.sort((a, b) => new Date(b.savedAt) - new Date(a.savedAt));

      const newTs = d.ts ?? 0;
      if (!force && newTs === _libTs) return null;   // nothing changed
      _libTs = newTs;
      _libCache(realServer);
      return realServer;
    } catch { return null; }
  }

  function startLibPoll() {
    if (_libPollTimer) return;
    _libPollTimer = setInterval(async () => {
      const updated = await libSync();
      if (updated !== null) renderLibrary();
    }, 3000);
  }

  function stopLibPoll() {
    clearInterval(_libPollTimer);
    _libPollTimer = null;
  }

  async function confirmSave() {
    const name = $('save-name').value.trim();
    if (!name || !S.tracks.length) return;
    const pl = {
      id: Date.now().toString(36),
      name,
      profile: S.profile,
      savedAt: new Date().toISOString(),
      tracks: S.tracks.map(t => ({
        ...t,
        _yt_cover: `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg`,
      })),
    };
    // optimistic local update
    const lib = libLoad();
    lib.unshift(pl);
    _libCache(lib);
    // sync to backend (relative URL — always hits local server regardless of ENGINE)
    fetch('/api/library', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(pl),
    }).catch(() => {});
    closeOvEl($('ov-save'));
    const btn = $('btn-save');
    saveBtnLabel('Salvata ✓');
    btn.classList.add('saved');
    setTimeout(() => { saveBtnLabel('Salva playlist'); btn.classList.remove('saved'); }, 2000);
  }

  function openSaveDialog() {
    if (!S.tracks.length) return;
    const defaultName = (S.profile ? (LABELS[S.profile] || S.profile) : 'Mix') +
      ' · ' + new Date().toLocaleDateString('it-IT', { day: '2-digit', month: 'short' });
    $('save-name').value = defaultName;
    openOv('ov-save');
    setTimeout(() => { $('save-name').focus(); $('save-name').select(); }, 100);
  }

  async function deletePlaylist(id) {
    _libCache(libLoad().filter(p => p.id !== id));
    fetch('/api/library/' + id, { method: 'DELETE' }).catch(() => {});
    renderLibrary();
  }

  function openPlaylist(pl) {
    S.tracks = pl.tracks;
    S.offline = true;
    S._isBestof = false;
    fetch(api('/api/audio/cache/clear'), { method: 'POST' }).catch(() => {});
    preloadBatch(pl.tracks, 0, 5);
    renderList();
    setStatus(`"${pl.name}" · ${pl.tracks.length} brani`);
    closeOvEl($('ov-library'));
    $('hero-circle').classList.remove('hidden');
    $('hero-cover').classList.remove('visible');
    $('profile-headline').style.display = '';
    $('profile-headline').innerHTML = esc(pl.name);
    $('now-title').style.display = 'none';
    $('now-artist').style.display = 'none';
    setTags([{ text: pl.tracks.length + ' brani' }, { text: new Date(pl.savedAt).toLocaleDateString('it-IT') }]);
    saveBtnLabel('Già in libreria');
    $('btn-save').classList.add('saved');
  }

  function renderLibrary() {
    const lib = libLoad();
    const bo = bestofLoad();
    const el = $('library-list');
    let html = '';

    if (bo && bo.tracks.length) {
      html += `
        <div class="pl-row pl-bestof" id="pl-bestof-row" style="cursor:pointer">
          <div style="width:44px;height:44px;border-radius:50%;background:var(--orange);flex:none"></div>
          <div class="pl-meta">
            <div class="pl-name" style="color:var(--orange)">${esc(_boLabel())}</div>
            <div class="pl-sub">${bo.tracks.length} brani · aggiornato ${new Date(bo.updatedAt).toLocaleDateString('it-IT')} · <em>live</em></div>
          </div>
        </div>`;
    }

    if (!lib.length && !bo) {
      el.innerHTML = '<p style="color:var(--ink2);font-size:13px;margin-bottom:0">Nessuna playlist salvata.</p>';
      return;
    }

    html += lib.map(pl => `
      <div class="pl-row">
        <div class="pl-meta" data-open="${esc(pl.id)}">
          <div class="pl-name">${esc(pl.name)}</div>
          <div class="pl-sub">${pl.tracks.length} brani · ${new Date(pl.savedAt).toLocaleDateString('it-IT')}</div>
        </div>
        <button class="pl-del" data-del="${esc(pl.id)}" title="Elimina">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
            <line x1="2" y1="2" x2="12" y2="12"/><line x1="12" y1="2" x2="2" y2="12"/>
          </svg>
        </button>
      </div>
    `).join('');

    el.innerHTML = html;

    const boRow = el.querySelector('#pl-bestof-row');
    if (boRow) boRow.onclick = openBestof;

    el.querySelectorAll('[data-open]').forEach(m => {
      m.onclick = () => openPlaylist(lib.find(p => p.id === m.dataset.open));
    });
    el.querySelectorAll('[data-del]').forEach(btn => {
      btn.onclick = e => {
        e.stopPropagation();
        if (confirm('Elimina "' + (lib.find(p => p.id === btn.dataset.del)?.name || '') + '"?'))
          deletePlaylist(btn.dataset.del);
      };
    });
  }

  /* ── HELPERS ──────────────────────────────────────────── */
  function setStatus(s) { $('status-row').textContent = s || ''; }
  function setProgress(p) { $('prog-i').style.width = (p || 0) + '%'; }
  function esc(s) { return (s || '').replace(/[&<>"]/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c])); }

  function setTags(tags) {
    const el = $('tag-row');
    el.innerHTML = tags.map(t =>
      `<div class="tag${t.hot ? ' hot' : ''}">${esc(t.text)}</div>`
    ).join('');
  }

  function openOv(id) { $(id).classList.add('on'); }
  function closeOvEl(el) { el.classList.remove('on'); }

  /* ── LISTENED ─────────────────────────────────────────── */
  async function refreshStats() {
    if (S.offline) return;
    try {
      const s = await (await fetch(api('/api/listened/stats'))).json();
      $('listened-stats').textContent = `${s.tracks} brani · ${s.videos} video in esclusione`;
    } catch {}
  }
  async function saveListened() {
    const fd = new FormData();
    fd.append('text', $('listened-text').value || '');
    const f = $('listened-file').files[0]; if (f) fd.append('file', f);
    try {
      const r = await fetch(api('/api/listened/import'), { method: 'POST', body: fd });
      const d = await r.json();
      $('listened-stats').textContent = `Aggiunti ${d.added}. Totale: ${d.total}.`;
      $('listened-text').value = '';
    } catch { $('listened-stats').textContent = 'Errore import.'; }
  }

  /* ── WIRING ───────────────────────────────────────────── */
  $('gen').onclick = generate;
  $('stop-btn').onclick = stopGen;
  $('btn-library').onclick = async () => {
    openOv('ov-library');
    renderLibrary();
    const updated = await libSync(true);
    if (updated !== null) renderLibrary();
    refreshStats();
  };
  $('btn-settings').onclick = () => { $('engine-url').value = ENGINE; refreshTaste(); openOv('ov-settings'); };

  /* ── Taste fine-tuning UI ─────────────────────────────────── */
  async function refreshTaste() {
    try {
      const r = await fetch('/api/taste/status');
      const s = await r.json();
      $('taste-toggle').checked = !!s.active;
      const sub = $('taste-sub');
      if (s.ready) {
        let msg = `Pronto · ${s.positives} ascolti positivi`;
        if (s.neg_active) {
          const conf = Math.round((s.neg_confidence || 0) * 100);
          msg += ` · negativi attivi (confidenza ${conf}%`;
          if (s.reproba_slots > 0) msg += `, re-proba ${s.reproba_slots} slot/gen`;
          msg += ')';
        }
        sub.textContent = msg;
      } else {
        sub.textContent = `In apprendimento · ${s.positives}/${s.min_pos} ascolti positivi prima di attivare l'effetto.`;
      }
    } catch {}
  }
  $('taste-toggle').onchange = async e => {
    await fetch('/api/taste/toggle', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: e.target.checked }),
    }).catch(() => {});
    refreshTaste();
  };
  $('taste-reset').onclick = async () => {
    if (!confirm('Azzerare la memoria d\'ascolto? I profili di partenza restano intatti.')) return;
    await fetch('/api/taste/reset', { method: 'POST' }).catch(() => {});
    refreshTaste();
  };
  $('btn-save').onclick = openSaveDialog;
  $('save-confirm').onclick = confirmSave;
  $('save-name').addEventListener('keydown', e => { if (e.key === 'Enter') confirmSave(); });
  $('listened-save').onclick = saveListened;
  $('engine-save').onclick = () => {
    localStorage.setItem('digmore_engine', $('engine-url').value.trim());
    location.reload();
  };
  document.querySelectorAll('[data-close]').forEach(b =>
    b.onclick = e => closeOvEl(e.target.closest('.ov'))
  );
  document.querySelectorAll('.ov').forEach(o =>
    o.onclick = e => { if (e.target === o) closeOvEl(o); }
  );

  // Stamp the running app version into the UI so we can confirm, from the
  // phone, that it isn't serving stale cached code.
  const _ver = $('ver');
  if (_ver) _ver.textContent = 'v' + APP_VERSION;

  // Popola il best of da playlist salvate se non esiste ancora
  if (!bestofLoad()) {
    const allTracks = libLoad().flatMap(pl => pl.tracks || []);
    if (allTracks.length) bestofUpdate(allTracks);
  }

  // Se il best of esiste in locale, caricalo subito sul server
  const _localBo = bestofLoad();
  if (_localBo && _localBo.tracks && _localBo.tracks.length) _bestofSyncServer(_localBo);

  // Sync iniziale + polling realtime ogni 5s
  libSync(true).then(updated => { if (updated !== null) renderLibrary(); });
  startLibPoll();

  loadProfiles();
})();
