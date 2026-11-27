// SPDX-License-Identifier: GPL-3.0-or-later
// A spectrum + waterfall tile (like one of the Radio tab's panels) in the top right corner of a page, on the
// range the page is working on. The page says what that is by defining window.miniSpecRange(), called once a
// second: it returns {lo, hi, label, marks: [{f, label}]} (Hz), or null for what the receiver sees now (the HF
// input or the VHF tuner's window). Helpers for it: miniSpec.voice(status, service) for the voice-session pages,
// miniSpec.around(f, mode) for a tuned SSB / AM / FM frequency.
// Data: /ws/panels (rxapp/panels.py), the Radio tab's feed. An HF range is live while the receiver is on HF; a
// VHF range is live while the tuner's 8.4 MHz window covers it; otherwise the tile shows its last picture.
// A mark {f, label, width, play: true} is the channel being listened to: drawn as a shaded band. A range may
// offer a closer look, {..., zoom: {lo, hi, label, marks}}: the tile then shows a zoom button that switches to it.
// A range may list segments, {..., segs: [{lo, hi, label, on}]}: shaded strips (at least a few pixels wide, so a
// 3 kHz FT8 segment still shows on a 50 MHz view) labelled with their band; on: the one picked, stronger. While a band scan
// runs, the range it is capturing is shaded too. Light / dark: spectheme.js, loaded before this.
// Include after the page's own script: <script src="spectheme.js"></script><script src="minispec.js"></script>.
'use strict';
(function () {
  const header = document.querySelector('header');
  if (!window.specTheme) { console.warn('minispec.js needs spectheme.js first'); return; }
  if (!header) return;
  const css = document.createElement('style');
  css.textContent = `
    header.mini{display:flex!important;gap:16px 24px;align-items:flex-start}
    header.mini>.mini-left{display:grid;gap:6px;flex:1 1 auto;min-width:0}
    .minispec{flex:0 0 616px;border:1px solid var(--spec-border);border-radius:6px;background:var(--spec-bg);overflow:hidden;
      color:var(--spec-text);font:11px ui-monospace,Menlo,monospace}
    .minispec .ms-bar{display:flex;gap:8px;align-items:center;padding:5px 8px;background:var(--spec-bar);border-bottom:1px solid var(--spec-border)}
    .minispec .ms-bar b{font:600 12px -apple-system,system-ui,sans-serif;color:var(--spec-strong);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .minispec .ms-st{margin-left:auto;white-space:nowrap;color:var(--spec-muted)}
    .minispec .ms-st.live{color:var(--spec-live)}
    .minispec canvas{display:block;width:100%}
    .minispec .ms-spec{height:120px} .minispec .ms-wf{height:105px}
    @media (max-width:1180px){header.mini{flex-direction:column}.minispec{flex:none;width:100%}}`;
  document.head.appendChild(css);
  const left = document.createElement('div');
  left.className = 'mini-left';
  while (header.firstChild) left.appendChild(header.firstChild);
  const box = document.createElement('div');
  box.className = 'minispec';
  box.innerHTML = '<div class="ms-bar"><b class="ms-label">Spectrum</b><span class="ms-st">connecting…</span></div><canvas class="ms-spec"></canvas><canvas class="ms-wf"></canvas>';
  header.classList.add('mini');
  header.append(left, box);
  const zoomBtn = document.createElement('button');
  zoomBtn.type = 'button';
  zoomBtn.className = 'ms-zoom';
  zoomBtn.style.cssText = 'font:600 11px/1 system-ui,sans-serif;padding:4px 8px;border-radius:4px;cursor:pointer;min-width:0;display:none;'
    + 'border:1px solid var(--spec-border);background:transparent;color:var(--spec-text)';
  zoomBtn.addEventListener('click', () => { M.zoomed = !M.zoomed; lastAsked = undefined; ask(); });
  box.querySelector('.ms-bar').append(zoomBtn, specTheme.button());
  const spec = box.querySelector('.ms-spec'), wf = box.querySelector('.ms-wf');
  const sctx = spec.getContext('2d'), wctx = wf.getContext('2d');
  const T = () => specTheme.t();
  const M = {want: null, auto: true, info: null, feed: null, cols: null, colT: null, frame: null,
             levels: {floor: -130, ceil: -60}, lastRow: 0, ws: null, sentKey: ''};
  const fmt = (f) => f >= 1e6 ? (f / 1e6).toFixed(f % 1e6 === 0 ? 0 : 3) + ' MHz' : (f / 1e3).toFixed(1) + ' kHz';

  function size() {
    const d = devicePixelRatio || 1;
    for (const c of [spec, wf]) {
      const w = Math.max(100, Math.floor(c.clientWidth * d)), h = Math.floor(c.clientHeight * d);
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    }
    M.cols = new Float32Array(spec.width).fill(NaN);
    M.colT = new Float64Array(spec.width);
    wctx.fillStyle = T().bg; wctx.fillRect(0, 0, wf.width, wf.height);
  }
  // the range on screen: the page's, or (until the page says) what the receiver sees now
  function range() {
    if (M.want) return M.want;
    const i = M.info;
    if (!i) return null;
    if (i.band === 'vhf' && i.vhf.center_hz) {
      const c = i.vhf.center_hz, s = i.vhf.span_hz;
      return {lo: c - s / 2, hi: c + s / 2, label: `VHF tuner window · ${fmt(c)}`};
    }
    return {lo: 0, hi: i.coverage_hz, label: 'HF input'};
  }
  const isHF = (r) => M.info && r.hi <= M.info.coverage_hz;
  // force: ask again even for the same ranges, which also brings back the last pictures rxapp kept (a VHF window
  // the tuner isn't on now shows how it looked when it was)
  function subscribe(force) {
    const r = range();
    if (!M.ws || M.ws.readyState !== 1) return;
    const ranges = r && isHF(r) ? [[Math.round(r.lo), Math.round(r.hi)]] : [];
    const key = JSON.stringify(ranges);
    if (key === M.sentKey && !force) return;
    M.sentKey = key;
    M.ws.send(JSON.stringify({ranges}));
  }
  function connect() {
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/panels');
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { M.sentKey = ''; subscribe(true); };
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        const was = range(), scanWas = JSON.stringify(M.info && M.info.scan);
        M.info = JSON.parse(e.data);
        if (JSON.stringify(M.info.scan) !== scanWas) { ask(); draw(range()); }   // a scan started, moved on or ended
        M.feed = (M.info.feeds[0] || {}).id;
        const now = range();
        const moved = !was || !now || was.lo !== now.lo || was.hi !== now.hi;
        if (moved) reset();
        subscribe(moved && !!was);
        return;
      }
      const dv = new DataView(e.data);
      const id = dv.getUint32(0, true), first = dv.getFloat64(4, true), bin = dv.getFloat32(12, true), n = dv.getUint32(16, true), t = dv.getFloat64(20, true);
      const r = range();
      if (!r || (isHF(r) ? id !== M.feed : id !== 0)) return;
      if (first + n * bin < r.lo || first > r.hi) return;
      overlay(r, {first, bin, n, t, db: new Float32Array(e.data, 28, n)});
      const live = Date.now() / 1000 - t < 3;
      if (live) { levels(); row(); }
      draw(r);
    };
    ws.onclose = () => { M.ws = null; setTimeout(connect, 2000); };
    M.ws = ws;
  }
  function reset() { if (M.cols) { M.cols.fill(NaN); M.colT.fill(0); } wctx.fillStyle = T().bg; wctx.fillRect(0, 0, wf.width, wf.height); draw(range()); }
  function overlay(r, fr) {
    const W = M.cols.length, span = r.hi - r.lo;
    for (let x = 0; x < W; x++) {
      const f0 = r.lo + x / W * span, f1 = r.lo + (x + 1) / W * span;
      let a = Math.floor((f0 - fr.first) / fr.bin), b = Math.ceil((f1 - fr.first) / fr.bin);
      if (b <= 0 || a >= fr.n) continue;
      a = Math.max(0, a); b = Math.min(fr.n, Math.max(a + 1, b));
      if (fr.t < M.colT[x]) continue;
      let v = -1e9; for (let i = a; i < b; i++) if (fr.db[i] > v) v = fr.db[i];
      M.cols[x] = v; M.colT[x] = fr.t;
    }
  }
  function levels() {
    const v = Array.from(M.cols).filter((x) => !isNaN(x)).sort((a, b) => a - b);
    if (v.length < 10) return;
    const floor = v[Math.floor(v.length * 0.1)], top = v[Math.floor(v.length * 0.998)];
    M.levels.floor += (floor - 3 - M.levels.floor) * 0.15;
    M.levels.ceil += (Math.max(top + 3, floor + 30) - M.levels.ceil) * 0.15;
  }
  function row() {
    const now = performance.now(); if (now - M.lastRow < 150) return; M.lastRow = now;
    const W = wf.width, H = wf.height;
    wctx.drawImage(wf, 0, 0, W, H - 1, 0, 1, W, H - 1);
    const img = wctx.createImageData(W, 1), d = img.data, {floor, ceil} = M.levels, k = 255 / (ceil - floor), tnow = Date.now() / 1000;
    for (let x = 0; x < W; x++) {
      const v = M.cols[x], ok = !isNaN(v) && tnow - M.colT[x] < 3, t = T(), L = t.lut;
      const c = ok ? Math.max(0, Math.min(255, Math.round((v - floor) * k))) * 3 : 0;
      d[x * 4] = ok ? L[c] : t.bgRGB[0]; d[x * 4 + 1] = ok ? L[c + 1] : t.bgRGB[1]; d[x * 4 + 2] = ok ? L[c + 2] : t.bgRGB[2]; d[x * 4 + 3] = 255;
    }
    wctx.putImageData(img, 0, 0);
  }
  function niceStep(x) { const p = 10 ** Math.floor(Math.log10(x)), m = x / p; return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p; }
  function draw(r) {
    if (!r) return;
    const t = T(), W = spec.width, H = spec.height, d = devicePixelRatio || 1, span = r.hi - r.lo, tnow = Date.now() / 1000;
    const {floor, ceil} = M.levels, X = (f) => (f - r.lo) / span * W;
    sctx.fillStyle = t.bg; sctx.fillRect(0, 0, W, H);
    sctx.strokeStyle = t.grid; sctx.lineWidth = 1;
    const step = niceStep(span / 5), ticks = [];
    for (let f = Math.ceil(r.lo / step) * step; f < r.hi; f += step) {
      const x = X(f);
      sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, H); sctx.stroke();
      const dec = step < 1e3 ? 4 : step < 1e4 ? 3 : step < 1e5 ? 2 : step < 1e6 ? 1 : 0;
      ticks.push([x, r.hi >= 1e6 ? (f / 1e6).toFixed(dec) + ' MHz' : (f / 1e3).toFixed(0) + ' kHz']);
    }
    for (let v = Math.ceil(floor / 10) * 10; v < ceil; v += 10) {
      const y = H - (v - floor) / (ceil - floor) * H;
      sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(W, y); sctx.stroke();
    }
    // a band scan: the range it is capturing now
    const sc = M.info && M.info.scan;
    if (sc && sc.lo != null && sc.hi > r.lo && sc.lo < r.hi) {
      const a = Math.max(0, X(sc.lo)), b = Math.min(W, X(sc.hi));
      sctx.fillStyle = t.scanFill; sctx.fillRect(a, 0, b - a, H);
      sctx.strokeStyle = t.scan; sctx.lineWidth = 2 * d; sctx.strokeRect(a + d, d, Math.max(0, b - a - 2 * d), H - 2 * d);
      sctx.font = `600 ${11 * d}px -apple-system, sans-serif`; sctx.fillStyle = t.scan;
      sctx.fillText('scanning', Math.min(a + 5 * d, W - 60 * d), 14 * d);
    }
    // band segments (the digital modes' decoding windows), labels on two rows so neighbours don't collide
    (r.segs || []).forEach((g, i) => {
      if (g.hi < r.lo || g.lo > r.hi) return;
      const a = X(g.lo), b = X(g.hi), w = Math.max(b - a, 4 * d), x0 = (a + b) / 2 - w / 2;
      sctx.fillStyle = g.on ? t.playFill : t.scanFill; sctx.fillRect(x0, 0, w, H);
      sctx.strokeStyle = g.on ? t.play : t.scan; sctx.lineWidth = d; sctx.strokeRect(x0, 0, w, H);
      if (g.label) {
        sctx.font = `600 ${10 * d}px -apple-system, sans-serif`; sctx.fillStyle = g.on ? t.play : t.scan;
        sctx.fillText(g.label, Math.min(x0 + w + 2 * d, W - 28 * d), (i % 2 ? 24 : 12) * d);
      }
    });
    // the channel being listened to (follow activity: whichever is playing), then the other marks
    sctx.font = `${10 * d}px -apple-system, sans-serif`;
    for (const m of r.marks || []) {
      if (m.f < r.lo || m.f > r.hi) continue;
      const x = X(m.f);
      if (m.play) {
        const half = Math.max((m.width || 12500) / 2 / span * W, 3 * d);
        sctx.fillStyle = t.playFill; sctx.fillRect(x - half, 0, 2 * half, H);
        sctx.strokeStyle = t.play; sctx.lineWidth = 2 * d; sctx.setLineDash([]);
        sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, H); sctx.stroke();
        sctx.font = `600 ${11 * d}px -apple-system, sans-serif`; sctx.fillStyle = t.play;
        sctx.fillText(m.label || 'listening', Math.min(x + half + 3 * d, W - 70 * d), 14 * d);
        sctx.font = `${10 * d}px -apple-system, sans-serif`;
        continue;
      }
      sctx.strokeStyle = m.color || t.mark; sctx.lineWidth = d; sctx.setLineDash([3 * d, 3 * d]);
      sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, H); sctx.stroke(); sctx.setLineDash([]);
      if (m.label) { sctx.fillStyle = m.color || t.mark; sctx.fillText(m.label, Math.min(x + 3 * d, W - 60 * d), 26 * d); }
    }
    sctx.strokeStyle = t.trace; sctx.lineWidth = 1.3 * d; sctx.beginPath();
    let pen = false, newest = 0;
    for (let x = 0; x < W; x++) {
      const v = M.cols[x];
      if (isNaN(v)) { pen = false; continue; }
      newest = Math.max(newest, M.colT[x]);
      const y = H - (v - floor) / (ceil - floor) * H;
      if (pen) sctx.lineTo(x, y); else { sctx.moveTo(x, y); pen = true; }
    }
    sctx.stroke();
    // the frequency legend last, highlighted (as on the Radio tab): a solid backing, so the trace never covers it
    sctx.font = `600 ${11 * d}px ui-monospace, "SF Mono", Menlo, monospace`;
    for (const [x, label] of ticks) {
      const tw = sctx.measureText(label).width, pad = 3 * d, bh = 14 * d, y0 = H - bh - 2 * d;
      const bx = Math.min(Math.max(0, x + 2 * d), W - tw - 2 * pad);
      sctx.fillStyle = t.legendBg; sctx.fillRect(bx, y0, tw + 2 * pad, bh);
      sctx.strokeStyle = t.legend; sctx.lineWidth = d;
      sctx.beginPath(); sctx.moveTo(x, H); sctx.lineTo(x, H - 6 * d); sctx.stroke();
      sctx.fillStyle = t.legend; sctx.shadowColor = t.legendGlow; sctx.shadowBlur = 6 * d;
      sctx.fillText(label, bx + pad, H - 6 * d);
      sctx.shadowBlur = 0;
    }
    const st = box.querySelector('.ms-st'), live = newest && tnow - newest < 3;
    box.querySelector('.ms-label').textContent = r.label || `${fmt(r.lo)} – ${fmt(r.hi)}`;
    box.querySelector('.ms-label').title = `${fmt(r.lo)} – ${fmt(r.hi)}`;
    st.className = 'ms-st' + (live ? ' live' : '');
    const why = !M.info ? 'connecting…' : isHF(r) ? (M.info.band === 'hf' ? 'waiting…' : 'receiver on VHF') : (M.info.band === 'vhf' ? 'outside the tuner window' : 'receiver on HF');
    const age = tnow - newest, ago = age < 5400 ? `${Math.round(age / 60)} min` : age < 172800 ? `${Math.round(age / 3600)} h` : `${Math.round(age / 86400)} days`;
    st.textContent = live ? '● live' : newest ? `${why} · picture ${ago} old` : why;
  }

  const mhz = (f) => (f / 1e6).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  let lastAsked = undefined;
  function ask() {
    if (typeof window.miniSpecRange !== 'function') return;
    let r = null;
    try { r = window.miniSpecRange(); } catch (e) { return; }
    if (same(r, lastAsked)) return;
    lastAsked = r;
    // the zoom button: only while the page offers a closer look; zoomed shows that instead
    const z = r && r.zoom;
    if (!z) M.zoomed = false;
    zoomBtn.style.display = z ? '' : 'none';
    zoomBtn.textContent = M.zoomed ? '⤢ Full band' : '🔍 Zoom in';
    zoomBtn.title = z ? (M.zoomed ? 'Back to the whole band' : `Zoom in on ${z.label || 'the station'}`) : '';
    zoomBtn.setAttribute('aria-pressed', String(!!M.zoomed));
    window.miniSpec.set(z && M.zoomed ? {...z, marks: z.marks || r.marks} : r);
  }

  window.miniSpec = {
    // a voice_session.py page (status from /api/<service>): the window it is listening on, with the channels on
    // the air; while the receiver is on another page's window, this page's own (first) window
    voice(st, service, extra) {
      const wins = (st && st.windows) || [];
      const cur = wins.find((w) => w.name === st.window && w.service === service);
      const w = cur || wins.find((x) => x.service === service);
      if (!w || !(w.hi > w.lo)) return null;
      const marks = cur ? Object.keys(st.keyed || {}).map((f) => ({f: +f, label: mhz(+f)})) : [];
      // the channel listened to (a play mark) is the zoom button's close-up: 200 kHz around it
      const p = (extra || []).find((m) => m.play);
      const zoom = p ? {lo: p.f - 100e3, hi: p.f + 100e3, label: `${mhz(p.f)} MHz · ${p.label || 'listening'}`, marks: [p]} : null;
      return {lo: w.lo, hi: w.hi, label: `${w.name} window · ${mhz(w.lo)}–${mhz(w.hi)} MHz`, marks: marks.concat(extra || []), zoom};
    },
    // the band scan running now: {band_lo, band_hi, lo, hi} (lo / hi: the part being captured), or null. A page
    // that started it shows the whole band while it runs: miniSpec.scanRange(label)
    scan: () => (M.info && M.info.scan) || null,
    scanRange(label) {
      const s = M.info && M.info.scan;
      if (!s) return null;
      const pad = (s.band_hi - s.band_lo) * 0.01;
      return {lo: s.band_lo - pad, hi: s.band_hi + pad, label: `${label || 'scanning'} · ${fmt(s.band_lo)} – ${fmt(s.band_hi)}`};
    },
    // around a tuned frequency: SSB shows its voice side, AM / FM both sides
    around(f, mode, label) {
      const side = {usb: [-1000, 4000], lsb: [-4000, 1000], cwu: [-1500, 2500], cwl: [-2500, 1500], am: [-10000, 10000],
                    wfm: [-200000, 200000], fm: [-50000, 50000], nfm: [-25000, 25000]}[mode] || [-5000, 5000];
      // the passband being listened to, shaded: SSB 300-2700 Hz to one side of the dial, AM / FM around it
      const pass = {usb: [300, 2700], lsb: [-2700, -300], cwu: [400, 600], cwl: [-600, -400], am: [-4500, 4500],
                    wfm: [-90000, 90000], fm: [-8000, 8000], nfm: [-6250, 6250]}[mode] || [-2500, 2500];
      return {lo: f + side[0], hi: f + side[1], label: label || `${fmt(f)} ${String(mode || '').toUpperCase()}`,
              marks: [{f: f + (pass[0] + pass[1]) / 2, width: pass[1] - pass[0], play: true, label: 'listening'}]};
    },
    // r: {lo, hi, label, marks} in Hz; null = what the receiver sees now
    set(r) {
      const was = range();
      M.want = r && r.hi > r.lo ? r : null;
      const now = range();
      if (!was || !now || was.lo !== now.lo || was.hi !== now.hi) { reset(); subscribe(true); } else draw(now);
    },
  };
  size();
  specTheme.onChange(() => { wctx.fillStyle = T().bg; wctx.fillRect(0, 0, wf.width, wf.height); draw(range()); });
  window.addEventListener('resize', () => { size(); draw(range()); });
  setInterval(() => { ask(); draw(range()); }, 1000);
  ask();                                                   // the page's script ran first: its range from the start
  setTimeout(ask, 300);
  connect();
})();
