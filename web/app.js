/* DIGMORE — frontend v3 */
(() => {
  const _DEFAULT_ENGINE = 'https://dsa222ffd-digmore-engine.hf.space';
  const ENGINE = (window.DIGMORE_ENGINE ||
    localStorage.getItem('digmore_engine') || _DEFAULT_ENGINE).replace(/\/$/, '');
  const api = p => ENGINE + p;

  const $ = id => document.getElementById(id);

  const S = {
    profile: null,     // selected profile id
    profiles: [],      // from /api/profiles
    jobId: null,
    poll: null,
    tracks: [],
    current: -1,
    playing: false,
    ytReady: false,
    ytPlayer: null,
  };

  // profile label map
  const LABELS = { soul: 'Chill Soul', jazz: 'Relax Jazz', ost: 'Relax OST', brazil: 'Brazil' };

  /* ── API ──────────────────────────────────────────────── */
  async function loadProfiles() {
    try {
      const r = await fetch(api('/api/profiles'));
      S.profiles = (await r.json()).profiles || [];
    } catch {
      setStatus('Engine non raggiungibile — imposta URL in "Engine".');
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
    // big orange circle, profile name as headline
    $('hero-circle').classList.remove('hidden');
    $('hero-cover').classList.remove('visible');
    $('profile-headline').innerHTML = p.label;
    $('now-title').style.display = 'none';
    $('now-artist').style.display = 'none';
    // tags: profile type
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
    S.tracks = []; renderList();
    setStatus('Avvio generazione…'); setProgress(0);
    const fd = new FormData();
    fd.append('profile', S.profile);
    fd.append('vibe_gate', 55);
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
      if (Array.isArray(j.results) && j.results.length) { S.tracks = j.results; renderList(); }
      if (j.status === 'done' || j.status === 'stopped' || j.status === 'error') {
        clearInterval(S.poll); S.poll = null;
        $('gen').disabled = false;
        $('stop-btn').style.display = 'none';
        setProgress(100);
        if (j.status === 'error') setStatus('Errore: ' + (j.error || '?'));
        else setStatus(`${S.tracks.length} brani trovati.`);
      }
    } catch {}
  }

  async function stopGen() {
    if (S.jobId) await fetch(api(`/api/generate/${S.jobId}/stop`), { method: 'POST' }).catch(() => {});
  }

  /* ── LIST ─────────────────────────────────────────────── */
  function coverSrc(t) {
    if (t.cover_image) return api('/api/cover?u=' + encodeURIComponent(t.cover_image));
    if (t.video_id) return `https://i.ytimg.com/vi/${t.video_id}/hqdefault.jpg`;
    return '';
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

      row.append(idx, img, meta, right);
      el.appendChild(row);
    });
  }

  /* ── PLAYER ───────────────────────────────────────────── */
  window.onYouTubeIframeAPIReady = () => {
    S.ytPlayer = new YT.Player('yt-player', {
      height: '1', width: '1',
      playerVars: { autoplay: 0, controls: 0, disablekb: 1, rel: 0, playsinline: 1, modestbranding: 1 },
      events: {
        onReady: () => { S.ytReady = true; },
        onStateChange: e => {
          if (e.data === YT.PlayerState.ENDED) play(S.current + 1);
          const p = $('play-btn');
          if (!p) return;
          if (e.data === YT.PlayerState.PLAYING) { S.playing = true; p.textContent = '⏸'; }
          if (e.data === YT.PlayerState.PAUSED)  { S.playing = false; p.textContent = '▶'; }
        }
      }
    });
  };

  function play(i) {
    if (i < 0 || i >= S.tracks.length) return;
    S.current = i;
    renderList();
    const t = S.tracks[i];

    // hero: show cover, update headline
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

    if (S.ytReady && S.ytPlayer) {
      S.ytPlayer.loadVideoById(t.video_id);
      S.ytPlayer.playVideo();
    }
    markListened(t);
    // scroll cover into view
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function togglePlay() {
    if (S.current < 0) { play(0); return; }
    if (!S.ytPlayer) return;
    S.playing ? S.ytPlayer.pauseVideo() : S.ytPlayer.playVideo();
  }

  async function markListened(t) {
    const fd = new FormData();
    fd.append('artist', t.artist || '');
    fd.append('title', t.title || '');
    fd.append('video_id', t.video_id || '');
    try { await fetch(api('/api/listened/mark'), { method: 'POST', body: fd }); } catch {}
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
  $('btn-listened').onclick = () => { openOv('ov-listened'); refreshStats(); };
  $('btn-settings').onclick = () => { $('engine-url').value = ENGINE; openOv('ov-settings'); };
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

  loadProfiles();
})();
