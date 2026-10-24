// SPDX-License-Identifier: GPL-3.0-or-later
// Radio page: a 2 x 2 grid of spectrum + waterfall panels, each on its own range. The receiver sees one input
// (HF, or one 8.4 MHz VHF tuner window) at a time: panels there are live, the others keep their last picture
// and say how old it is. Feeds come from /ws/panels (rxapp/panels.py). Loaded before app.js, whose globals
// (state, tune, switchBand, api, flash, ...) it uses once the page is running.
'use strict';

const PANEL_PRESETS = [
  {lo: 0, hi: 2e6, label: '0–2 MHz (LF / MW)'},
  {lo: 2e6, hi: 30e6, label: '2–30 MHz (HF)'},
  {lo: 530e3, hi: 1.71e6, label: 'AM broadcast'},
  {lo: 3.5e6, hi: 4e6, label: '80 / 75 m'},
  {lo: 5.9e6, hi: 6.2e6, label: '49 m shortwave'},
  {lo: 7e6, hi: 7.3e6, label: '40 m'},
  {lo: 9.4e6, hi: 9.9e6, label: '31 m shortwave'},
  {lo: 14e6, hi: 14.35e6, label: '20 m'},
  {lo: 26.96e6, hi: 27.41e6, label: 'CB'},
  {lo: 28e6, hi: 29.7e6, label: '10 m'},
  {lo: 50e6, hi: 54e6, label: '6 m'},
  {lo: 88e6, hi: 108e6, label: 'FM broadcast'},
  {lo: 118e6, hi: 137e6, label: 'Airband'},
  {lo: 144e6, hi: 148e6, label: '2 m'},
  {lo: 150e6, hi: 174e6, label: 'VHF-high (public safety, marine)'},
  {lo: 156e6, hi: 163e6, label: 'Marine VHF'},
  {lo: 162.3e6, hi: 162.6e6, label: 'NOAA weather'},
  {lo: 400e6, hi: 470e6, label: 'UHF 400–470 MHz'},
  {lo: 420e6, hi: 450e6, label: '70 cm'},
  {lo: 462e6, hi: 468e6, label: 'FRS / GMRS'},
];
const PANEL_DEFAULTS = [0, 1, 12, 17].map((i) => PANEL_PRESETS[i]);
const PANEL_LIVE_S = 3;                     // a column counts as live if updated this recently
const PANEL_LUT = (() => {                  // viridis-like
  const stops = [[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164]];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = i / 255 * (stops.length - 1), k = Math.min(Math.floor(x), stops.length - 2), f = x - k;
    for (let c = 0; c < 3; c++) lut[i * 3 + c] = stops[k][c] + (stops[k + 1][c] - stops[k][c]) * f;
  }
  return lut;
})();
const PG = {panels: [], ws: null, info: null, feedOf: {}};   // feedOf: "lo:hi" -> feed id

const pfmt = (f) => f >= 1e6 ? (f / 1e6).toLocaleString(undefined, {maximumFractionDigits: 3}) + ' MHz' : (f / 1e3).toLocaleString(undefined, {maximumFractionDigits: 1}) + ' kHz';
const pAgo = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`; };
const pInput = (r) => (r.hi <= (PG.info ? PG.info.coverage_hz : state.info.max_freq_hz) ? 'hf' : 'vhf');

// What a panel shows: its range, or with Fit (VHF panels) the tuner's 8.4 MHz window: the live one when it is
// in this panel's range, else the last window this panel saw.
function viewOf(p) {
  const r = p.range;
  if (r.zoom) return {lo: r.zoom[0], hi: r.zoom[1], zoomed: true};
  if (!r.fit || pInput(r) !== 'vhf' || !PG.info) return r;
  const span = PG.info.vhf.span_hz, c0 = PG.info.vhf.center_hz;
  let c = null;
  if (PG.info.band === 'vhf' && c0 && c0 + span / 2 > r.lo && c0 - span / 2 < r.hi) c = c0;
  else {
    let newest = null;
    for (const fr of p.frames.values()) if (fr.id === 0 && (!newest || fr.t > newest.t)) newest = fr;
    if (newest) c = newest.first + newest.n * newest.bin / 2;
  }
  return c == null ? r : {lo: c - span / 2, hi: c + span / 2, window: true};
}
// a new view (Fit toggled, the tuner moved): redraw every column from the frames kept
function ensureView(p) {
  const v = viewOf(p), key = `${v.lo}:${v.hi}`;
  if (key === p.viewKey) return;
  const old = p.viewKey && p.v;
  p.viewKey = key; p.v = v;
  p.cols.fill(NaN); p.colT.fill(0);
  const W = p.wf.width, H = p.wf.height;
  if (old && old.hi > old.lo) {
    const tmp = document.createElement('canvas'); tmp.width = W; tmp.height = H;
    tmp.getContext('2d').drawImage(p.wf, 0, 0);
    const span = v.hi - v.lo, dx = (old.lo - v.lo) / span * W, dw = (old.hi - old.lo) / span * W;
    p.wctx.fillStyle = '#05070a'; p.wctx.fillRect(0, 0, W, H);
    p.wctx.imageSmoothingEnabled = false;
    p.wctx.drawImage(tmp, 0, 0, W, H, dx, 0, dw, H);
  } else { p.wctx.fillStyle = '#05070a'; p.wctx.fillRect(0, 0, W, H); }
  [...p.frames.values()].sort((a, b) => a.t - b.t).forEach((fr) => overlay(p, fr));
}

function loadRanges() {
  try { const v = JSON.parse(localStorage.getItem('rx.panels')); if (Array.isArray(v) && v.length === 4) return v; } catch (e) { /* defaults */ }
  return PANEL_DEFAULTS.map((p) => ({...p}));
}
function saveRanges() { try { localStorage.setItem('rx.panels', JSON.stringify(PG.panels.map((p) => p.range))); } catch (e) { /* private window */ } }

// ------------------------------------------------------------------ building the grid
function initPanels() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  PG.panels = loadRanges().map((range, idx) => makePanel(grid, range, idx));
  window.addEventListener('resize', () => PG.panels.forEach((p) => { sizePanel(p); rebuild(p); }));
  connectPanels();
  setInterval(() => PG.panels.forEach(drawStatus), 1000);
}
function makePanel(grid, range, idx) {
  const el = document.createElement('div');
  el.className = 'pnl';
  const opts = PANEL_PRESETS.map((p, i) => `<option value="${i}">${p.label}</option>`).join('') + '<option value="custom">Custom…</option>';
  el.innerHTML = `<div class="pnl-bar"><select aria-label="Panel ${idx + 1} range">${opts}</select>
      <span class="custom"><input class="lo" inputmode="decimal" aria-label="From (MHz)"> – <input class="hi" inputmode="decimal" aria-label="To (MHz)"> MHz <button type="button" class="go">Set</button></span>
      <button type="button" class="fit" title="Zoom this panel to the VHF tuner's 8.4 MHz live window (it follows the tuner)">Fit 8.4 MHz</button>
      <button type="button" class="unzoom" title="Back to the panel's whole range" style="display:none"></button>
      <span class="pnl-st"></span></div>
    <canvas class="spec"></canvas><canvas class="wf"></canvas>`;
  grid.appendChild(el);
  const p = {idx, range, el, spec: el.querySelector('.spec'), wf: el.querySelector('.wf'), frames: new Map(),
             cols: null, colT: null, levels: {floor: -130, ceil: -60}, lastRow: 0};
  p.sctx = p.spec.getContext('2d'); p.wctx = p.wf.getContext('2d');
  const sel = el.querySelector('select'), custom = el.querySelector('.custom');
  const preset = PANEL_PRESETS.findIndex((x) => x.lo === range.lo && x.hi === range.hi);
  sel.value = preset >= 0 ? String(preset) : 'custom';
  custom.classList.toggle('on', preset < 0);
  el.querySelector('.lo').value = range.lo / 1e6; el.querySelector('.hi').value = range.hi / 1e6;
  sel.addEventListener('change', () => {
    if (sel.value === 'custom') { custom.classList.add('on'); el.querySelector('.lo').focus(); return; }
    custom.classList.remove('on');
    setRange(p, {...PANEL_PRESETS[+sel.value]});
  });
  el.querySelector('.go').addEventListener('click', () => {
    const lo = parseFloat(el.querySelector('.lo').value) * 1e6, hi = parseFloat(el.querySelector('.hi').value) * 1e6;
    if (!(hi > lo) || lo < 0) return flash('range: from < to, in MHz (e.g. 7.0 – 7.3)');
    const cov = PG.info ? PG.info.coverage_hz : state.info.max_freq_hz;
    if (hi > cov && lo < 24e6) return flash(`a range must be all on HF (0–${cov / 1e6} MHz) or all on the VHF tuner (24–1700 MHz)`);
    if (hi > 1700e6) return flash('the VHF tuner stops at 1700 MHz');
    setRange(p, {lo, hi, label: `${pfmt(lo)} – ${pfmt(hi)}`});
  });
  const fit = el.querySelector('.fit');
  fit.addEventListener('click', () => { p.range = {...p.range, fit: !p.range.fit, zoom: null}; saveRanges(); drawPanel(p); subscribeSoon(); });
  el.querySelector('.unzoom').addEventListener('click', () => unzoom(p));
  wirePanelMouse(p);
  sizePanel(p);
  return p;
}
function sizePanel(p) {
  const d = devicePixelRatio || 1;
  for (const c of [p.spec, p.wf]) {
    const w = Math.max(100, Math.floor(c.clientWidth * d)), h = Math.floor(c.clientHeight * d);
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  }
  p.cols = new Float32Array(p.spec.width).fill(NaN);
  p.colT = new Float64Array(p.spec.width);
  p.viewKey = null; p.v = p.range;
  p.wctx.fillStyle = '#05070a'; p.wctx.fillRect(0, 0, p.wf.width, p.wf.height);
}
function setRange(p, range) {
  p.range = {...range, fit: !!p.range.fit, zoom: null}; p.frames.clear(); sizePanel(p); saveRanges(); subscribe(); drawPanel(p);
}

// ------------------------------------------------------------------ data
function connectPanels() {
  const ws = new WebSocket(wsUrl('/ws/panels'));
  ws.binaryType = 'arraybuffer';
  ws.onopen = subscribe;
  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {
      PG.info = JSON.parse(e.data);
      PG.feedOf = {};
      for (const f of PG.info.feeds) PG.feedOf[`${f.lo}:${f.hi}`] = f.id;
      return;
    }
    const dv = new DataView(e.data);
    const id = dv.getUint32(0, true), first = dv.getFloat64(4, true), bin = dv.getFloat32(12, true), n = dv.getUint32(16, true), t = dv.getFloat64(20, true);
    const fr = {id, first, bin, n, t, db: new Float32Array(e.data, 28, n)};
    for (const p of PG.panels) {
      const mine = id === 0 ? pInput(p.range) === 'vhf' : PG.feedOf[subRange(p).join(':')] === id;   // its own HF feed only
      if (!mine || first + n * bin < p.range.lo || first > p.range.hi) continue;
      p.frames.set(id === 0 ? 'v' + Math.round(first / 1e3) : 'h' + id, fr);
      if (p.frames.size > 80) { const old = [...p.frames.entries()].sort((a, b) => a[1].t - b[1].t)[0][0]; p.frames.delete(old); }
      const was = p.viewKey; ensureView(p);
      if (p.viewKey === was) overlay(p, fr);
      if (Date.now() / 1000 - t < PANEL_LIVE_S) { waterfallRow(p); drawPanel(p); } else drawPanel(p);
    }
  };
  ws.onclose = () => { PG.ws = null; setTimeout(connectPanels, 1500); };
  PG.ws = ws;
}
function subRange(p) {
  const v = p.range.zoom && pInput(p.range) === 'hf' ? p.range.zoom : [p.range.lo, p.range.hi];
  return [Math.round(v[0]), Math.round(v[1])];
}
function subscribe() {
  if (!PG.ws || PG.ws.readyState !== 1) return;
  PG.ws.send(JSON.stringify({ranges: PG.panels.map(subRange)}));
}
let subTimer = null;
function subscribeSoon() { clearTimeout(subTimer); subTimer = setTimeout(subscribe, 300); }   // zoom bursts: one request
// put a frame's bins onto the panel's pixel columns (max per column, so narrow carriers show)
function overlay(p, fr) {
  const W = p.cols.length, lo = p.v.lo, span = p.v.hi - p.v.lo;
  for (let x = 0; x < W; x++) {
    const f0 = lo + x / W * span, f1 = lo + (x + 1) / W * span;
    let a = Math.floor((f0 - fr.first) / fr.bin), b = Math.ceil((f1 - fr.first) / fr.bin);
    if (b <= 0 || a >= fr.n) continue;
    a = Math.max(0, a); b = Math.min(fr.n, Math.max(a + 1, b));
    if (fr.t < p.colT[x]) continue;                     // an older picture than what this column has
    let v = -1e9; for (let i = a; i < b; i++) if (fr.db[i] > v) v = fr.db[i];
    p.cols[x] = v; p.colT[x] = fr.t;
  }
}
function rebuild(p) { p.viewKey = null; ensureView(p); drawPanel(p); }

// ------------------------------------------------------------------ drawing
function autoLevelsP(p) {
  const v = Array.from(p.cols).filter((x) => !isNaN(x)).sort((a, b) => a - b);
  if (v.length < 10) return;
  const floor = v[Math.floor(v.length * 0.1)], top = v[Math.floor(v.length * 0.998)];
  p.levels.floor += (floor - 3 - p.levels.floor) * 0.15;
  p.levels.ceil += (Math.max(top + 3, floor + 30) - p.levels.ceil) * 0.15;
}
function waterfallRow(p) {
  const now = performance.now(); if (now - p.lastRow < 150) return; p.lastRow = now;
  const W = p.wf.width, H = p.wf.height, ctx = p.wctx;
  ctx.drawImage(p.wf, 0, 0, W, H - 1, 0, 1, W, H - 1);
  const row = ctx.createImageData(W, 1), d = row.data, tnow = Date.now() / 1000;
  const {floor, ceil} = p.levels, k = 255 / (ceil - floor);
  for (let x = 0; x < W; x++) {
    const v = p.cols[x];
    if (isNaN(v)) { d[x * 4 + 3] = 255; continue; }
    const c = Math.max(0, Math.min(255, (v - floor) * k)) | 0, live = tnow - p.colT[x] < PANEL_LIVE_S, a = live ? 1 : 0.3;
    d[x * 4] = PANEL_LUT[c * 3] * a; d[x * 4 + 1] = PANEL_LUT[c * 3 + 1] * a; d[x * 4 + 2] = PANEL_LUT[c * 3 + 2] * a; d[x * 4 + 3] = 255;
  }
  ctx.putImageData(row, 0, 0);
}
function drawPanel(p) {
  ensureView(p);
  if (document.getElementById('autoscale').checked) autoLevelsP(p);
  const ctx = p.sctx, W = p.spec.width, H = p.spec.height, d = devicePixelRatio || 1;
  const {floor, ceil} = p.levels, lo = p.v.lo, hi = p.v.hi, span = hi - lo, tnow = Date.now() / 1000;
  ctx.fillStyle = '#05070a'; ctx.fillRect(0, 0, W, H);
  ctx.font = `${10 * d}px -apple-system, sans-serif`; ctx.strokeStyle = '#1b222b'; ctx.fillStyle = '#5b6573'; ctx.lineWidth = 1;
  const step = niceStep(span / 6), ticks = [];
  for (let f = Math.ceil(lo / step) * step; f < hi; f += step) {
    const x = (f - lo) / span * W;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    // one unit per panel: MHz once the range reaches 1 MHz, with as many decimals as the step needs
    ticks.push([x, hi >= 1e6 ? (f / 1e6).toFixed(step < 1e4 ? 3 : step < 1e5 ? 2 : step < 1e6 ? 1 : 0) : (f / 1e3).toFixed(0) + 'k']);
  }
  for (let v = Math.ceil(floor / 10) * 10; v < ceil; v += 10) {
    const y = H - (v - floor) / (ceil - floor) * H;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
  // the trace: bright where live, grey where it is the last picture
  for (const live of [false, true]) {
    ctx.strokeStyle = live ? '#4cc2ff' : '#55606c'; ctx.lineWidth = d; ctx.beginPath();
    let pen = false;
    for (let x = 0; x < W; x++) {
      const v = p.cols[x], isLive = tnow - p.colT[x] < PANEL_LIVE_S;
      if (isNaN(v) || isLive !== live) { pen = false; continue; }
      const y = H - (v - floor) / (ceil - floor) * H;
      pen ? ctx.lineTo(x, y) : ctx.moveTo(x, y); pen = true;
    }
    ctx.stroke();
  }
  // the receiver's VHF window on VHF panels
  if (PG.info && PG.info.band === 'vhf' && pInput(p.range) === 'vhf' && PG.info.vhf.center_hz) {
    const a = (PG.info.vhf.center_hz - PG.info.vhf.span_hz / 2 - lo) / span * W, b = (PG.info.vhf.center_hz + PG.info.vhf.span_hz / 2 - lo) / span * W;
    if (b > 0 && a < W) { ctx.fillStyle = 'rgba(108,201,154,0.07)'; ctx.fillRect(a, 0, b - a, H); }
  }
  // channels
  for (const ch of state.channels || []) {
    if (ch.band !== pInput(p.range) || ch.freq_hz < lo || ch.freq_hz > hi) continue;
    if (ch.kind !== 'listen' && ch.ssrc !== state.selected) continue;
    const sel = ch.ssrc === state.selected, x = (ch.freq_hz - lo) / span * W;
    const bw = ch.mode === 'wfm' ? 200000 : ch.mode === 'nfm' ? 12500 : ch.mode.startsWith('cw') ? 400 : ch.mode === 'usb' || ch.mode === 'lsb' ? 3000 : 10000;
    const off = ch.mode === 'usb' ? 0 : ch.mode === 'lsb' ? -bw : -bw / 2;
    const x0 = (ch.freq_hz + off - lo) / span * W, x1 = (ch.freq_hz + off + bw - lo) / span * W;
    ctx.fillStyle = sel ? 'rgba(255,184,76,0.18)' : 'rgba(76,194,255,0.10)'; ctx.fillRect(x0, 0, Math.max(1, x1 - x0), H);
    ctx.strokeStyle = sel ? '#ffb84c' : '#4cc2ff'; ctx.lineWidth = (sel ? 2 : 1) * d;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }
  // frequency marks last, so the trace never covers them: bright yellow on a dark backing
  ctx.font = `600 ${11 * d}px ui-monospace, "SF Mono", Menlo, monospace`; ctx.textBaseline = 'alphabetic';
  for (const [x, label] of ticks) {
    const tw = ctx.measureText(label).width, pad = 3 * d, bh = 14 * d, y0 = H - bh - 2 * d;
    const bx = Math.min(Math.max(0, x + 2 * d), W - tw - 2 * pad);
    ctx.fillStyle = 'rgba(5,7,10,0.85)'; ctx.fillRect(bx, y0, tw + 2 * pad, bh);
    ctx.strokeStyle = '#ffd84a'; ctx.lineWidth = d;
    ctx.beginPath(); ctx.moveTo(x, H); ctx.lineTo(x, H - 6 * d); ctx.stroke();
    ctx.fillStyle = '#ffd84a'; ctx.shadowColor = 'rgba(255,216,74,0.6)'; ctx.shadowBlur = 6 * d;
    ctx.fillText(label, bx + pad, H - 6 * d);
    ctx.shadowBlur = 0;
  }
  drawStatus(p);
}
function drawStatus(p) {
  const st = p.el.querySelector('.pnl-st'), tnow = Date.now() / 1000;
  let live = 0, newest = 0, seen = 0;
  for (let x = 0; x < p.colT.length; x++) {
    const t = p.colT[x]; if (!t) continue;
    seen++; if (tnow - t < PANEL_LIVE_S) live++; else newest = Math.max(newest, t);
  }
  const W = p.colT.length, want = pInput(p.range), band = PG.info ? PG.info.band : state.band;
  // where the receiver is, when it isn't looking at this panel
  const tc = PG.info && PG.info.vhf.center_hz;
  const away = band !== want ? `receiver on ${band.toUpperCase()}`
    : want === 'vhf' && tc ? `tuner at ${pfmt(tc)}` : '';
  let txt, cls;
  if (live >= W * 0.98) { txt = '● live'; cls = 'live'; }
  else if (live) { txt = `● live in part${newest ? ' · rest ' + pAgo(newest) + ' ago' : ''}`; cls = 'part'; }
  else if (seen) { txt = `seen ${pAgo(newest)} ago${away ? ' · ' + away : ''}`; cls = 'old'; }
  else if (away) { txt = `not seen yet · ${away}, outside this range · click to look`; cls = 'old'; }
  else { txt = 'starting…'; cls = 'old'; }
  const fit = p.el.querySelector('.fit');
  fit.style.display = want === 'vhf' ? '' : 'none';
  fit.classList.toggle('on', !!p.range.fit);
  fit.setAttribute('aria-pressed', !!p.range.fit);
  const z = p.el.querySelector('.unzoom');
  z.style.display = p.range.zoom ? '' : 'none';
  if (p.range.zoom) z.textContent = `zoom ${pfmt(p.range.zoom[0])}–${pfmt(p.range.zoom[1])} ✕`;
  if (p.range.fit && want === 'vhf' && !p.range.zoom) txt += p.v && p.v.window ? ` · window ${pfmt(p.v.lo)}–${pfmt(p.v.hi)}` : ' · Fit zooms in once the tuner has been here';
  st.textContent = txt; st.className = 'pnl-st ' + cls;
  st.title = want === 'vhf' ? 'VHF tuner panel: live only inside the tuner\'s 8.4 MHz window' : 'HF input panel';
}
function panelsRedraw() { for (const p of PG.panels) drawPanel(p); }

// ------------------------------------------------------------------ tuning from a panel
// HF band plan for a click: what voice there is usually sent as. Elsewhere the current mode stays.
const HF_PLAN = [
  [530e3, 1710e3, 'am'],                                     // AM broadcast
  [1.8e6, 2.0e6, 'lsb'], [3.6e6, 4.0e6, 'lsb'], [7.125e6, 7.3e6, 'lsb'],   // amateur phone, LSB below 10 MHz
  [5.33e6, 5.41e6, 'usb'],                                   // 60 m channels
  [14.15e6, 14.35e6, 'usb'], [18.11e6, 18.168e6, 'usb'], [21.2e6, 21.45e6, 'usb'], [24.93e6, 24.99e6, 'usb'],
  [28.3e6, 29.3e6, 'usb'], [29.5e6, 29.7e6, 'nfm'],          // 10 m: SSB, then the FM repeaters
  [26.965e6, 27.405e6, 'am'],                                // CB: AM (SSB users mostly on channel 38 LSB)
  [2.3e6, 2.5e6, 'am'], [3.2e6, 3.4e6, 'am'], [4.75e6, 5.06e6, 'am'], [5.9e6, 6.2e6, 'am'], [7.3e6, 7.45e6, 'am'],
  [9.4e6, 9.9e6, 'am'], [11.6e6, 12.1e6, 'am'], [13.57e6, 13.87e6, 'am'], [15.1e6, 15.8e6, 'am'],
  [17.48e6, 17.9e6, 'am'], [21.45e6, 21.85e6, 'am'], [25.67e6, 26.1e6, 'am'],   // shortwave broadcast
];
function modeFor(f, band) {
  if (band === 'vhf') return f >= 87.5e6 && f <= 108e6 ? 'wfm' : f >= 108e6 && f < 137e6 ? 'am' : 'nfm';
  const plan = HF_PLAN.find(([lo, hi]) => f >= lo && f <= hi);
  if (plan) return plan[2];
  const m = current() && current().mode;
  return m && state.info.band_modes.hf.includes(m) && m !== 'nfm' ? m : 'am';   // NFM off-plan on HF: rarely right
}
function snapPanel(p, x) {
  const W = p.cols.length, k = Math.round(6 * (devicePixelRatio || 1));
  let best = -1, bv = -1e9;
  for (let i = Math.max(0, x - k); i <= Math.min(W - 1, x + k); i++) if (p.cols[i] > bv) { bv = p.cols[i]; best = i; }
  return best >= 0 && bv > p.levels.floor + 12 ? best : x;
}
async function tuneFromPanel(p, x) {
  const W = p.cols.length, xi = snapPanel(p, Math.round(x));
  let f = p.v.lo + (xi + 0.5) / W * (p.v.hi - p.v.lo);
  const want = pInput(p.range), step = +$('step').value;
  const fresh = state.band !== want;
  const mode = modeFor(f, want);
  f = Math.round(f / step) * step;
  try {
    const out = want === 'vhf' && PG.info && PG.info.vhf.center_hz && Math.abs(f - PG.info.vhf.center_hz) > PG.info.vhf.span_hz / 2;
    if (fresh || out) {                     // moving the receiver: hold the voice session off it
      const fo = await api('GET', '/api/voice/focus');
      if (fo.focus !== 'pause') { await api('POST', '/api/voice/focus', {focus: 'pause'}); flash('voice session paused for manual tuning (button at the top resumes it)'); }
    }
    if (fresh) { state.switching = true; try { await switchBand(want); } finally { state.switching = false; } }
    $('mode').value = mode; $('step').value = defaultStep(mode, f);
    tune(f, mode);
  } catch (e) { flash(e.message); }
}
// factor > 1 zooms out; the frequency under the pointer (at fraction frac of the width) stays put
function zoomPanel(p, frac, factor) {
  ensureView(p);
  const v = p.v, f = v.lo + frac * (v.hi - v.lo), hf = pInput(p.range) === 'hf';
  const cov = PG.info ? PG.info.coverage_hz : state.info.max_freq_hz;
  const [minLo, maxHi] = hf ? [0, cov] : [p.range.lo, p.range.hi];    // HF can zoom out to the whole input
  const minSpan = hf ? 5e3 : 100e3;                                   // VHF bins are 4 kHz: no finer than that
  let span = Math.max(minSpan, Math.min(maxHi - minLo, (v.hi - v.lo) * factor));
  let lo = f - frac * span, hi = lo + span;
  if (lo < minLo) { lo = minLo; hi = lo + span; }
  if (hi > maxHi) { hi = maxHi; lo = hi - span; }
  const full = !hf && lo <= p.range.lo + 1 && hi >= p.range.hi - 1 || hf && Math.abs(lo - p.range.lo) < 1 && Math.abs(hi - p.range.hi) < 1;
  p.range = {...p.range, zoom: full ? null : [lo, hi]};
  saveRanges(); drawPanel(p); subscribeSoon();
}
// Shift + ← / → (or the dial): zoom the panel last pointed at, else the one showing the tuned frequency,
// around the tuned frequency when it is in view
function panelZoomKey(dir) {
  const ch = current();
  const p = PG.panels[PG.active] || PG.panels.find((q) => ch && q.v && ch.freq_hz >= q.v.lo && ch.freq_hz <= q.v.hi && ch.band === pInput(q.range)) || PG.panels[0];
  if (!p) return null;
  ensureView(p);
  const f = ch && ch.band === pInput(p.range) && ch.freq_hz >= p.v.lo && ch.freq_hz <= p.v.hi ? ch.freq_hz : (p.v.lo + p.v.hi) / 2;
  zoomPanel(p, (f - p.v.lo) / (p.v.hi - p.v.lo), dir > 0 ? 0.8 : 1.25);
  return `panel ${p.idx + 1}: ${pfmt(p.v.lo)} – ${pfmt(p.v.hi)}`;
}
function unzoom(p) { p.range = {...p.range, zoom: null}; saveRanges(); drawPanel(p); subscribeSoon(); }
function wirePanelMouse(p) {
  p.el.addEventListener('pointerenter', () => { PG.active = p.idx; });
  for (const c of [p.spec, p.wf]) {
    c.addEventListener('click', (e) => {
      const r = c.getBoundingClientRect();
      tuneFromPanel(p, (e.clientX - r.left) / r.width * p.cols.length);
    });
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      const dy = e.deltaY || e.deltaX;          // macOS turns Shift + wheel into a sideways scroll
      let held = false; try { held = !!(window.parent.rxCtrl && window.parent.rxCtrl.held.shift); } catch (err) { /* standalone */ }
      if (e.shiftKey || held || e.ctrlKey) {    // ctrlKey: a trackpad pinch; held: Shift on the Mac while a dial scrolls
        const r = c.getBoundingClientRect();
        zoomPanel(p, (e.clientX - r.left) / r.width, Math.exp(Math.max(-0.5, Math.min(0.5, dy * (e.deltaMode ? 0.05 : 0.002)))));
        return;
      }
      const ch = current(); if (!ch || ch.band !== pInput(p.range)) return;
      stepTune(dy < 0 ? 1 : -1);
    }, {passive: false});
  }
}
