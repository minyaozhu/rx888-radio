// rxapp browser client: tuning, audio, spectrum/waterfall, channels, scans, recordings, dashboard.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (method, path, body) => {
  const r = await fetch(path, {method, headers: body ? {'Content-Type': 'application/json'} : {},
                               body: body ? JSON.stringify(body) : undefined});
  if (!r.ok) throw new Error((await r.json().catch(() => ({detail: r.statusText}))).detail);
  return r.json();
};
const wsUrl = (p) => (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + p;
const fmtKHz = (hz) => (hz / 1e3).toLocaleString(undefined, {minimumFractionDigits: hz % 1000 ? 2 : 0, maximumFractionDigits: 2});
const fmtMHz = (hz) => (hz / 1e6).toFixed(hz % 1000 ? 4 : 3);
// The tuning box shows kHz on HF and MHz on VHF; tables pick by size.
const fmtTune = (hz) => state.band === 'vhf' ? fmtMHz(hz) : fmtKHz(hz);
const fmtHz = (hz) => hz >= 30e6 ? fmtMHz(hz) + ' MHz' : fmtKHz(hz) + ' kHz';
const fmtTime = (t) => new Date(t * 1000).toLocaleString();

const state = {
  info: null, channels: [], selected: null,   // selected = ssrc we tune/listen
  view: {center: 1.1e6, bin: 1000, n: 2048},
  band: 'hf',
  levels: {floor: -130, ceil: -60},
  listening: false,
};

// ------------------------------------------------------------------ setup
async function init() {
  state.info = await api('GET', '/api/info');
  for (const b of Object.keys(state.info.bands)) {
    $('schedBand').add(new Option(b, b));
    $('dashBand').add(new Option(b, b));
  }
  state.channels = await api('GET', '/api/channels');
  await applyBand(state.info.band);
  wire();
  resizeCanvases();
  openSpectrum();
  openStatus();
  refreshRecordings();
}

// Show the controls for the selected input and pick (or make) a channel on it.
async function applyBand(band) {
  state.band = band;
  $('bandHF').classList.toggle('active', band === 'hf');
  $('bandVHF').classList.toggle('active', band === 'vhf');
  $('unit').textContent = band === 'vhf' ? 'MHz' : 'kHz';
  $('vgaWrap').style.display = band === 'vhf' ? '' : 'none';
  $('hfGainWrap').style.display = band === 'hf' ? '' : 'none';
  renderHfGain(state.info.hf_gain?.db ?? null);
  $('coverage').value = String(state.info.adc_rate_hz);
  $('vga').value = state.info.vhf.vga; $('vgaVal').textContent = state.info.vhf.vga;
  const modes = state.info.band_modes[band];
  $('mode').innerHTML = ''; for (const m of modes) $('mode').add(new Option(m.toUpperCase(), m));
  $('band').innerHTML = '';
  for (const [k, b] of Object.entries(state.info.bands)) if (b.band === band) $('band').add(new Option(k, k));
  buildZoom();
  wctx.fillStyle = '#05070a'; wctx.fillRect(0, 0, wf.width, wf.height);   // old band's waterfall
  const mine = state.channels.filter((c) => c.kind === 'listen' && c.band === band);
  let main = mine.find((c) => !c.parked) || mine[0];      // prefer a channel that's playing
  if (!main) {
    const dflt = band === 'vhf' ? {freq_hz: state.info.vhf.center_hz || 97.4e6, mode: 'wfm'} : {freq_hz: 860e3, mode: 'am'};
    main = await api('POST', '/api/channels', {...dflt, label: 'main', band});
    state.channels.push(main);
  }
  select(main.ssrc, main);
}

async function switchBand(band) {
  if (band === state.band) return;
  for (const b of ['bandHF', 'bandVHF']) $(b).disabled = true;
  try {
    state.info = await api('POST', '/api/band', {band});
    state.channels = await api('GET', '/api/channels');
    await applyBand(band);
  } catch (e) { flash(e.message); }
  finally { for (const b of ['bandHF', 'bandVHF']) $(b).disabled = false; }
}

// Span choices: what the input can show (HF: up to Nyquist; VHF: the tuner window).
// Same span choices on both inputs, up to the coverage. On VHF, spans wider than the tuner
// window are built by sweeping the tuner (slower updates, VHF audio paused).
function buildZoom() {
  const maxSpan = state.info.max_freq_hz;
  const live = 2 * state.info.vhf.window_hz;
  const spans = [100e3, 200e3, 500e3, 1e6, 2e6, 5e6, 10e6, 20e6].filter((x) => x < maxSpan * 0.98);
  $('zoom').innerHTML = '';
  const tag = (sp) => state.band === 'vhf' && sp > live + 1 ? ' (sweep)' : '';
  for (const sp of spans) $('zoom').add(new Option((sp >= 1e6 ? sp / 1e6 + ' MHz' : sp / 1e3 + ' kHz') + tag(sp), sp));
  const full = state.band === 'vhf' ? `${Math.floor(maxSpan / 1e6)} MHz (sweep)` : `0–${Math.floor(maxSpan / 1e6)} MHz (full)`;
  $('zoom').add(new Option(full, maxSpan));
  if (state.band === 'vhf') $('zoom').add(new Option(`${(live / 1e6).toFixed(1)} MHz (tuner, live)`, live));
  syncZoom();
}

function renderHfGain(db) {
  const auto = db === null || db === undefined;
  $('hfAuto').checked = auto;
  $('hfGain').disabled = auto;
  if (!auto) $('hfGain').value = db;
  $('hfGainVal').textContent = auto ? 'auto' : `${(+$('hfGain').value).toFixed(1)} dB`;
}

function syncZoom() {
  const span = state.view.bin * state.view.n;
  let best = null, bd = Infinity;
  for (const o of $('zoom').options) { const d = Math.abs(Math.log(+o.value / span)); if (d < bd) { bd = d; best = o; } }
  if (best) $('zoom').value = best.value;
}

function select(ssrc, ch) {
  state.selected = ssrc;
  ch = ch || state.channels.find((c) => c.ssrc === ssrc);
  if (!ch) return;
  $('freq').value = fmtTune(ch.freq_hz);
  $('mode').value = ch.mode;
  $('step').value = defaultStep(ch.mode, ch.freq_hz);
  if (state.listening) startAudio();
  renderChannels();
}

const defaultStep = (mode, f) => mode === 'wfm' ? 100000 : f >= 30e6 ? (mode === 'am' ? 25000 : 12500) :
  (mode === 'am' && f < 1.71e6) ? 10000 : mode === 'am' ? 5000 : mode === 'nfm' ? 5000 : mode.startsWith('cw') ? 10 : 100;

const current = () => state.channels.find((c) => c.ssrc === state.selected);

// ------------------------------------------------------------------ tuning
function parseFreq(s) {
  s = s.trim().toLowerCase().replace(/,/g, '');
  const m = s.match(/^([\d.]+)\s*(m|mhz|k|khz|hz)?$/);
  if (!m) return null;
  const v = parseFloat(m[1]);
  if (m[2] && m[2].startsWith('m')) return v * 1e6;
  if (m[2] === 'hz') return v;
  if (m[2] && m[2].startsWith('k')) return v * 1e3;
  // bare numbers: on HF < 50 means MHz (else kHz); on VHF < 2000 means MHz
  return (state.band === 'vhf' ? v < 2000 : v < 50) ? v * 1e6 : v * 1e3;
}

let tuneTimer = null, pendingTune = null;
async function tune(freq_hz, mode) {
  const ch = current();
  if (!ch) return;
  if (freq_hz != null) {
    const [lo, hi] = state.band === 'vhf' ? state.info.vhf_range_hz : [1e3, state.info.max_freq_hz - 1e3];
    freq_hz = Math.max(lo, Math.min(freq_hz, hi));
    ch.freq_hz = freq_hz;
    $('freq').value = fmtTune(freq_hz);
    keepInView(freq_hz);
  }
  if (mode) ch.mode = mode;
  drawSpectrumOverlay();
  // coalesce wheel/drag bursts into one request every 80 ms
  pendingTune = {freq_hz: ch.freq_hz, mode: ch.mode};
  if (tuneTimer) return;
  tuneTimer = setTimeout(async () => {
    tuneTimer = null;
    const body = pendingTune; pendingTune = null;
    try {
      await api('PATCH', `/api/channels/${ch.ssrc}`, body);
      if (mode && state.listening) startAudio();
    } catch (e) { flash(e.message); }
  }, 80);
}

function keepInView(f) {
  const span = state.view.bin * state.view.n;
  const lo = state.view.center - span / 2, hi = state.view.center + span / 2;
  if (f < lo + span * 0.05 || f > hi - span * 0.05) setView(f, null);
}

let viewTimer = null;
function setView(center, bin) {
  clearTimeout(viewTimer);
  viewTimer = setTimeout(() => api('POST', '/api/spectrum', {center_hz: center, bin_hz: bin}).catch((e) => flash(e.message)), 120);
}

function flash(msg) {
  $('scanStatus').textContent = msg;
  console.warn(msg);
}

// ------------------------------------------------------------------ audio
let audio = {ctx: null, gain: null, ws: null, next: 0};

function startAudio() {
  stopAudio(false);
  if (!audio.ctx) {
    audio.ctx = new AudioContext();
    audio.gain = audio.ctx.createGain();
    audio.gain.connect(audio.ctx.destination);
  }
  audio.ctx.resume();
  audio.gain.gain.value = parseFloat($('volume').value);
  audio.next = 0;
  const ws = new WebSocket(wsUrl(`/ws/audio/${state.selected}`));
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    const rate = new DataView(e.data).getUint32(0, true);
    const pcm = new Int16Array(e.data, 4);
    if (!pcm.length) return;
    const buf = audio.ctx.createBuffer(1, pcm.length, rate);
    const out = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 32768;
    const src = audio.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(audio.gain);
    const now = audio.ctx.currentTime;
    if (audio.next < now + 0.05 || audio.next > now + 1.0) audio.next = now + 0.2;  // (re)start jitter buffer
    src.start(audio.next);
    audio.next += buf.duration;
  };
  ws.onclose = () => {   // server restart or network blip: reconnect while still listening
    if (audio.ws === ws && state.listening) setTimeout(() => state.listening && startAudio(), 1000);
  };
  audio.ws = ws;
  state.listening = true;
  $('listen').textContent = '■ Stop';
  $('listen').classList.add('on');
}

function stopAudio(updateButton = true) {
  if (audio.ws) { const w = audio.ws; audio.ws = null; w.onmessage = null; w.onclose = null; w.close(); }
  if (updateButton) {
    state.listening = false;
    $('listen').textContent = '▶ Listen';
    $('listen').classList.remove('on');
  }
}

// ------------------------------------------------------------------ spectrum + waterfall
const spec = $('spectrum'), wf = $('waterfall');
const sctx = spec.getContext('2d'), wctx = wf.getContext('2d');
let lastFrame = null;

// viridis-like palette
const LUT = (() => {
  const stops = [[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164]];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = i / 255 * (stops.length - 1), k = Math.min(Math.floor(x), stops.length - 2), f = x - k;
    for (let c = 0; c < 3; c++) lut[i * 3 + c] = stops[k][c] + (stops[k + 1][c] - stops[k][c]) * f;
  }
  return lut;
})();

function resizeCanvases() {
  for (const c of [spec, wf]) {
    const w = Math.floor(c.clientWidth * devicePixelRatio);
    if (c.width !== w) c.width = w;
  }
  wf.height = Math.floor(300 * devicePixelRatio);
  spec.height = Math.floor(160 * devicePixelRatio);
}
window.addEventListener('resize', resizeCanvases);

function openSpectrum() {
  const ws = new WebSocket(wsUrl('/ws/spectrum'));
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    const dv = new DataView(e.data);
    const center = dv.getFloat64(0, true), bin = dv.getFloat32(8, true), n = dv.getUint32(12, true);
    const db = new Float32Array(e.data, 16, n);
    if (center !== state.view.center || bin !== state.view.bin) {
      state.view = {center, bin, n};
      syncZoom();
    }
    state.view.n = n;
    lastFrame = db;
    if ($('autoscale').checked) autoLevels(db);
    drawWaterfallLine(db);
    drawSpectrum();
  };
  ws.onclose = () => setTimeout(openSpectrum, 1000);
}

function autoLevels(db) {
  const s = Float32Array.from(db).sort();
  const floor = s[Math.floor(s.length * 0.1)], top = s[Math.floor(s.length * 0.999)];
  state.levels.floor += (floor - 3 - state.levels.floor) * 0.1;
  state.levels.ceil += (Math.max(top + 3, floor + 30) - state.levels.ceil) * 0.1;
}

function drawWaterfallLine(db) {
  const W = wf.width, H = wf.height;
  wctx.drawImage(wf, 0, 0, W, H - 1, 0, 1, W, H - 1);   // scroll down one row
  const row = wctx.createImageData(W, 1), d = row.data, n = db.length;
  const {floor, ceil} = state.levels, k = 255 / (ceil - floor);
  for (let x = 0; x < W; x++) {
    // max over the bins that fall in this pixel, so narrow carriers stay visible
    const a = Math.floor(x * n / W), b = Math.max(a + 1, Math.floor((x + 1) * n / W));
    let v = -1e9; for (let i = a; i < b; i++) if (db[i] > v) v = db[i];
    const c = Math.max(0, Math.min(255, (v - floor) * k)) | 0;
    d[x * 4] = LUT[c * 3]; d[x * 4 + 1] = LUT[c * 3 + 1]; d[x * 4 + 2] = LUT[c * 3 + 2]; d[x * 4 + 3] = 255;
  }
  wctx.putImageData(row, 0, 0);
}

const xToFreq = (x, w) => state.view.center + (x / w - 0.5) * state.view.bin * state.view.n;
const freqToX = (f, w) => ((f - state.view.center) / (state.view.bin * state.view.n) + 0.5) * w;

function drawSpectrum() {
  const W = spec.width, H = spec.height, db = lastFrame;
  sctx.fillStyle = '#05070a'; sctx.fillRect(0, 0, W, H);
  const {floor, ceil} = state.levels, span = state.view.bin * state.view.n;
  // frequency grid
  const step = niceStep(span / 8);
  sctx.font = `${11 * devicePixelRatio}px -apple-system, sans-serif`;
  sctx.fillStyle = '#5b6573'; sctx.strokeStyle = '#1b222b';
  for (let f = Math.ceil((state.view.center - span / 2) / step) * step; f < state.view.center + span / 2; f += step) {
    const x = freqToX(f, W);
    sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, H); sctx.stroke();
    sctx.fillText(f >= 1e6 ? (f / 1e6).toFixed(step < 1e5 ? 3 : 1) + ' M' : (f / 1e3).toFixed(0) + ' k', x + 3, H - 4);
  }
  // dB grid
  for (let v = Math.ceil(floor / 10) * 10; v < ceil; v += 10) {
    const y = H - (v - floor) / (ceil - floor) * H;
    sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(W, y); sctx.stroke();
    sctx.fillText(v + ' dB', 3, y - 2);
  }
  if (db) {
    sctx.strokeStyle = '#4cc2ff'; sctx.lineWidth = devicePixelRatio; sctx.beginPath();
    const n = db.length;
    for (let x = 0; x < W; x++) {
      const a = Math.floor(x * n / W), b = Math.max(a + 1, Math.floor((x + 1) * n / W));
      let v = -1e9; for (let i = a; i < b; i++) if (db[i] > v) v = db[i];
      const y = H - (v - floor) / (ceil - floor) * H;
      x ? sctx.lineTo(x, y) : sctx.moveTo(x, y);
    }
    sctx.stroke();
  }
  drawSpectrumOverlay();
  $('viewInfo').textContent = `${fmtHz(Math.max(0, state.view.center - span / 2))} – ${fmtHz(state.view.center + span / 2)} · ${Math.round(state.view.bin)} Hz bins` +
    (state.band === 'vhf' ? (state.sweeping ? ` · sweep, ${state.sweepSec ? state.sweepSec.toFixed(1) + ' s/update' : 'starting…'} · VHF audio paused (click a signal to listen)`
                                            : ` · tuner ${fmtMHz(state.info.vhf.center_hz)} MHz`) : '');
}

function drawSpectrumOverlay() {
  const W = spec.width, H = spec.height;
  for (const ch of state.channels) {
    if (ch.band !== state.band || ch.parked) continue;
    const x = freqToX(ch.freq_hz, W);
    if (x < 0 || x > W) continue;
    const sel = ch.ssrc === state.selected;
    // passband
    const bw = ch.mode === 'wfm' ? 200000 : ch.mode === 'nfm' ? 12500 : ch.mode.startsWith('cw') ? 400 : ch.mode === 'usb' || ch.mode === 'lsb' ? 3000 : 10000;
    const lo = ch.mode === 'usb' ? 0 : ch.mode === 'lsb' ? -bw : -bw / 2;
    const x0 = freqToX(ch.freq_hz + lo, W), x1 = freqToX(ch.freq_hz + lo + bw, W);
    sctx.fillStyle = sel ? 'rgba(255,184,76,0.15)' : 'rgba(76,194,255,0.10)';
    sctx.fillRect(x0, 0, Math.max(1, x1 - x0), H);
    sctx.strokeStyle = sel ? '#ffb84c' : '#4cc2ff'; sctx.lineWidth = devicePixelRatio;
    sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, H); sctx.stroke();
  }
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

// One tuning step (or n steps) up/down, snapped to the step grid
function stepTune(dir, n = 1) {
  const ch = current(); if (!ch) return;
  const step = +$('step').value;
  tune(Math.round(ch.freq_hz / step) * step + dir * n * step);
}

// Move a click onto the strongest signal within +-frac of the view, if one stands out.
function snapToPeak(f, frac) {
  const db = lastFrame; if (!db) return f;
  const n = db.length, span = state.view.bin * n;
  const i0 = Math.round((f - state.view.center) / state.view.bin + n / 2), w = Math.max(1, Math.round(frac * n));
  let best = -1, bv = -1e9;
  for (let i = Math.max(0, i0 - w); i <= Math.min(n - 1, i0 + w); i++) if (db[i] > bv) { bv = db[i]; best = i; }
  if (best < 0 || bv < state.levels.floor + 12) return f;   // nothing clearly above the noise
  return state.view.center + (best - n / 2) * state.view.bin;
}

// mouse: click tunes (snapping to a nearby signal), wheel steps, drag pans
function wireScope(canvas) {
  let drag = null;
  canvas.addEventListener('mousedown', (e) => { drag = {x: e.clientX, center: state.view.center, moved: false}; });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) > 4) drag.moved = true;
    if (drag.moved) setView(drag.center - dx / canvas.clientWidth * state.view.bin * state.view.n, null);
  });
  window.addEventListener('mouseup', (e) => {
    if (drag && !drag.moved && e.target === canvas) {
      const r = canvas.getBoundingClientRect();
      const step = +$('step').value;
      tune(Math.round(snapToPeak(xToFreq(e.clientX - r.left, r.width), 8 / r.width) / step) * step);
    }
    drag = null;
  });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const ch = current(); if (!ch) return;
    const step = +$('step').value;
    tune(Math.round(ch.freq_hz / step) * step + (e.deltaY < 0 ? step : -step));
  }, {passive: false});
}

// ------------------------------------------------------------------ status, channels
function openStatus() {
  const ws = new WebSocket(wsUrl('/ws/status'));
  ws.onmessage = (e) => {
    const s = JSON.parse(e.data);
    state.info.vhf = s.vhf; state.info.max_freq_hz = s.max_freq_hz; state.info.adc_rate_hz = s.adc_rate_hz;
    state.sweeping = !!s.spectrum?.sweeping; state.sweepSec = s.spectrum?.sweep_sec;
    if (!state.gainDragging && (s.hf_gain_db ?? null) !== (state.info.hf_gain?.db ?? null)) {
      state.info.hf_gain = {...state.info.hf_gain, db: s.hf_gain_db ?? null}; renderHfGain(s.hf_gain_db ?? null);
    }
    if (s.band !== state.band && !state.switching) {   // another page switched the input
      state.channels = s.channels; applyBand(s.band); return;
    }
    // keep local edits (pending tunes) authoritative for the selected channel
    const sel = current();
    if (sel && !s.channels.some((c) => c.ssrc === sel.ssrc) && !state.recreating) {
      // our channel is gone (rxapp restarted): make a new one where we were tuned
      state.recreating = true;
      api('POST', '/api/channels', {freq_hz: sel.freq_hz, mode: sel.mode, label: 'main', band: sel.band})
        .then((c) => { state.channels.push(c); select(c.ssrc, c); })
        .finally(() => { state.recreating = false; });
      return;
    }
    state.channels = s.channels.map((c) => (sel && c.ssrc === sel.ssrc && pendingTune) ? {...c, ...pendingTune} : c);
    renderHealth(s.health, s.scanning);
    renderMeter();
    renderChannels();
  };
  ws.onclose = () => { $('health').innerHTML = '<span class="bad">disconnected</span>'; setTimeout(openStatus, 1500); };
}

function renderHealth(h, scanning) {
  if (!h || !h.t) return;
  const ok = h.ok ? '<span class="ok">● radiod</span>' : `<span class="bad">● radiod not answering (${h.fails || 0})</span>`;
  const rate = h.input_samprate ? (h.input_samprate / 1e6).toFixed(1) + ' MS/s' : '';
  const over = h.overloads_recent ? `<span class="bad">ADC overloading (${h.overloads_recent} in 5 s)</span>` : 'no overloads';
  const input = h.band === 'vhf' ? `VHF tuner ${h.vhf?.center_hz ? fmtMHz(h.vhf.center_hz) : '?'} MHz${h.vhf?.locked === false ? ' <span class="bad">PLL unlocked</span>' : ''}` : 'HF';
  $('health').innerHTML = `${ok} · ${input} · ${rate} · IF ${h.if_power_db?.toFixed(1)} dBFS · RF gain ${h.rf_gain_db?.toFixed(1)} dB · ${over}` +
    ` · ${h.spectrum_fps} fps · ${h.channels} ch` + (h.restarts ? ` · restarts ${h.restarts}` : '') + (scanning ? ' · <b>scanning</b>' : '');
}

function renderMeter() {
  const ch = current(); if (!ch) return;
  const p = ch.power_db;
  if (p == null) return;
  // S-units on HF: S9 = -73 dBm, 6 dB per S unit. radiod's baseband power is dBFS-ish, so this is relative.
  const pct = Math.max(0, Math.min(100, (p + 130) / 90 * 100));
  $('meterfill').style.width = pct + '%';
  $('meterText').textContent = `${p.toFixed(1)} dB · SNR ${ch.snr_db != null ? ch.snr_db.toFixed(1) : '—'} dB`;
  $('record').classList.toggle('on', !!ch.recording);
  $('record').textContent = ch.recording ? '■ Stop rec' : '● Rec';
}

function renderChannels() {
  const rows = state.channels.filter((c) => c.band === state.band && c.kind !== 'ft8' && c.kind !== 'sstv').map((c) => `
    <tr class="${c.ssrc === state.selected ? 'sel' : ''}">
      <td>${fmtHz(c.freq_hz)}${c.parked ? ' <span class="dim">(paused: outside tuner window)</span>' : ''}</td><td>${c.mode.toUpperCase()}</td><td>${c.label || ''}${c.recording ? ' <span style="color:var(--rec)">● rec</span>' : ''}</td>
      <td>${c.power_db != null ? c.power_db.toFixed(1) + ' dB' : ''}</td>
      <td>${c.snr_db != null ? `<span class="snrbar" style="width:${Math.max(2, Math.min(80, c.snr_db * 1.5))}px"></span>${c.snr_db.toFixed(1)}` : ''}</td>
      <td><button data-act="sel" data-ssrc="${c.ssrc}">Tune/listen</button>
          <button data-act="del" data-ssrc="${c.ssrc}">✕</button></td>
    </tr>`).join('');
  $('channelRows').innerHTML = rows;
}

// ------------------------------------------------------------------ scans
let autoScanTimer = null;
async function runScan() {
  const band = $('band').value;
  $('scanBtn').disabled = true;
  $('scanStatus').textContent = `scanning ${band}…`;
  try {
    const r = await api('POST', `/api/scan/${band}`);
    $('scanStatus').textContent = `${r.hits.length} signals · floor ${r.floor_db?.toFixed(1) ?? '—'} dB · ${r.frames} frames at ${r.bin_hz} Hz · ${new Date().toLocaleTimeString()}`;
    const mode = r.mode;
    $('scanRows').innerHTML = r.hits.map((h) => `
      <tr><td>${fmtHz(h.freq_hz)}</td>
      <td><span class="snrbar" style="width:${Math.max(2, Math.min(120, h.snr_db * 2))}px"></span>${h.snr_db} dB</td>
      <td><button data-act="tune" data-f="${h.freq_hz}" data-m="${mode}">Tune</button>
          <button data-act="add" data-f="${h.freq_hz}" data-m="${mode}">+ Channel</button></td></tr>`).join('');
  } catch (e) {
    $('scanStatus').textContent = 'scan failed: ' + e.message;
  } finally {
    $('scanBtn').disabled = false;
  }
}

// ------------------------------------------------------------------ recordings
async function refreshRecordings() {
  const recs = await api('GET', '/api/recordings');
  $('recRows').innerHTML = recs.map((r) => `
    <tr><td>${fmtTime(r.start)}</td><td>${fmtHz(r.freq_hz)}</td><td>${(r.mode || '').toUpperCase()}</td>
    <td>${r.seconds != null ? r.seconds.toFixed(0) + ' s' : '<span style="color:var(--rec)">recording…</span>'}</td>
    <td>${r.peak_dbfs != null ? r.peak_dbfs.toFixed(1) + ' dBFS' : ''}</td>
    <td>${r.seconds != null ? `<audio controls preload="none" src="/api/recordings/${r.id}/audio"></audio>` : ''}</td></tr>` +
    (r.transcript ? `<tr><td></td><td colspan="5" class="dim">“${escapeHtml(r.transcript)}”</td></tr>` : '')).join('');
}

const escapeHtml = (t) => t.replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

// ------------------------------------------------------------------ monitor (server-side jobs)
async function refreshMonitor() {
  const [sch, wat] = await Promise.all([api('GET', '/api/schedules'), api('GET', '/api/watches')]);
  $('schedRows').innerHTML = sch.map((s) => `<tr><td>${s.band}</td><td>${s.every_min} min</td>
    <td>${s.last_run ? fmtTime(s.last_run) : 'pending'}</td>
    <td><button data-act="delsched" data-id="${s.id}">✕</button></td></tr>`).join('');
  $('watchRows').innerHTML = wat.map((w) => `<tr><td>${fmtHz(w.freq_hz)}</td><td>${w.mode.toUpperCase()}</td>
    <td>${w.label || ''}</td><td>${w.snr_db} dB${w.transcribe ? ' · transcribe' : ''}</td>
    <td>${w.snr_now != null ? w.snr_now.toFixed(1) + ' dB' : '—'}</td>
    <td>${w.recording ? '<span style="color:var(--rec)">● recording</span>' : 'waiting'} · ${w.recordings_this_run} saved</td>
    <td><button data-act="delwatch" data-id="${w.id}">✕</button></td></tr>`).join('');
}
let monitorTimer = null;

// ---------------------------------------------------------------- SSTV
let sstvTimer = null;
const sstv = {info: null, ids: null};

function wireSstv() {
  $('sstvToggle').addEventListener('click', async () => {
    $('sstvToggle').disabled = true;
    try { sstv.info = await api('POST', '/api/sstv', {on: !(sstv.info && sstv.info.running)}); }
    catch (e) { flash(e.message); }
    $('sstvToggle').disabled = false;
    refreshSstv();
  });
  $('sstvRows').addEventListener('click', (e) => {     // hear a frequency on the main receiver
    const b = e.target.closest('button'); if (!b) return;
    if (state.band !== 'hf') return flash('switch to HF first');
    $('mode').value = b.dataset.mode; tune(+b.dataset.f, b.dataset.mode);
  });
  $('sstvGallery').addEventListener('click', async (e) => {
    const del = e.target.closest('button[data-del]');
    if (del && confirm('Delete this picture?')) { await api('DELETE', `/api/sstv/pictures/${del.dataset.del}`); sstv.ids = null; refreshSstv(); }
    const img = e.target.closest('img'); if (img) window.open(img.src, '_blank');
  });
}

async function refreshSstv() {
  try {
    const i = sstv.info = await api('GET', '/api/sstv');
    $('sstvToggle').textContent = i.running ? 'Stop SSTV monitor' : 'Start SSTV monitor';
    $('sstvToggle').classList.toggle('on', i.running);
    $('sstvRows').innerHTML = i.channels.map((c) => `<tr><td>${c.band}</td><td>${fmtHz(c.freq_hz)} ${c.mode.toUpperCase()}</td>
      <td>${c.parked ? '<span class="dim">paused (VHF selected)</span>' : c.state.startsWith('capturing') ? `<span style="color:var(--rec)">● ${c.state}</span>` : c.state}</td>
      <td class="dim">${c.last && c.last.sync != null ? `sync ${c.last.sync.toFixed(2)} · video ${c.last.video.toFixed(2)}${c.last.mode !== '?' && c.last.sync >= 0.25 ? ' · ' + c.last.mode : ''}` : ''}</td>
      <td>${c.triggers}</td><td>${c.pictures}</td>
      <td><button data-f="${c.freq_hz}" data-mode="${c.mode}">Listen</button></td></tr>`).join('')
      || '<tr><td colspan="7" class="dim">Not running.</td></tr>';
    const pics = await api('GET', '/api/sstv/pictures?limit=200');
    const ids = pics.map((p) => p.id).join();
    if (ids !== sstv.ids) {
      sstv.ids = ids;
      $('sstvCount').textContent = pics.length ? `(${pics.length})` : '';
      $('sstvGallery').innerHTML = pics.map((p) => `<figure>
        <img src="/api/sstv/pictures/${p.id}.png" alt="SSTV picture, ${p.mode}, ${fmtTime(p.t)}" loading="lazy">
        <figcaption><span>${fmtTime(p.t)}</span><span>${fmtHz(p.freq_hz)} · ${p.mode.replace(/_/g, ' ')}${p.complete ? '' : ' · <span class="partial">partial</span>'}</span>
          <span><a href="/api/sstv/pictures/${p.id}.wav" download>audio</a> <button data-del="${p.id}">delete</button></span></figcaption>
      </figure>`).join('') || '<p class="dim">No pictures yet. SSTV is busiest on weekend daytimes on 14.230 MHz.</p>';
    }
  } catch (e) { console.warn('sstv', e); }
}

// ---------------------------------------------------------------- FT8
let ft8Timer = null;
const ft8 = {info: null, rows: [], since: 0, view: 'msgs', stations: {}};
const utc = (t) => new Date(t * 1000).toISOString().slice(11, 19);
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);

function wireFt8() {
  $('ft8Toggle').addEventListener('click', async () => {
    const on = !(ft8.info && ft8.info.running);
    const bands = [...document.querySelectorAll('#ft8Bands input:checked')].map((x) => x.value);
    $('ft8Toggle').disabled = true;
    try { ft8.info = await api('POST', '/api/ft8', {on, bands: on ? bands : null}); }
    catch (e) { flash(e.message); }
    $('ft8Toggle').disabled = false;
    renderFt8Info();
  });
  document.querySelectorAll('[data-ft8view]').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('[data-ft8view]').forEach((x) => x.classList.toggle('active', x === b));
    ft8.view = b.dataset.ft8view;
    $('ft8Msgs').style.display = ft8.view === 'msgs' ? '' : 'none';
    $('ft8Qso').style.display = ft8.view === 'qso' ? '' : 'none';
    refreshFt8(true);
  }));
  $('ft8Filter').addEventListener('change', () => refreshFt8(true));
  $('ft8BandRows').addEventListener('click', (e) => {   // hear a band: the main receiver goes there in USB
    const b = e.target.closest('button'); if (!b) return;
    const band = ft8.info.bands.find((x) => x.band === b.dataset.band);
    if (state.band !== 'hf') return flash('switch to HF first');
    $('mode').value = 'usb'; tune(band.dial_hz, 'usb');
  });
  $('ft8Find').addEventListener('input', () => ft8.view === 'msgs' ? renderFt8Rows() : refreshFt8(true));
}

function renderFt8Info() {
  const i = ft8.info; if (!i) return;
  $('ft8Toggle').textContent = i.running ? 'Stop FT8 monitor' : 'Start FT8 monitor';
  $('ft8Toggle').classList.toggle('on', i.running);
  const active = new Set(i.bands.map((b) => b.band));
  const chips = $('ft8Bands');
  if (!chips.dataset.built || chips.dataset.built !== i.available.join()) {
    chips.innerHTML = i.available.map((b) => `<label><input type="checkbox" value="${b}" checked> ${b}</label>`).join('');
    chips.dataset.built = i.available.join();
    $('ft8Filter').innerHTML = '<option value="">all</option>' + i.available.map((b) => `<option>${b}</option>`).join('');
  }
  if (i.running) chips.querySelectorAll('input').forEach((x) => { x.checked = active.has(x.value); });
  chips.querySelectorAll('input').forEach((x) => { x.disabled = i.running; x.parentElement.classList.toggle('off', !x.checked); });
  const next = i.slot_sec - (Date.now() / 1000) % i.slot_sec;
  $('ft8Status').textContent = !i.decoder ? 'decode_ft8 is missing: build tools/ft8_lib (see README).'
    : i.running ? `Decoding ${i.bands.length} bands · next slot ends in ${next.toFixed(0)} s · runs on the server, keeps going with this page closed.`
    : 'Decodes FT8 on every chosen band at once, every 15 s slot. Runs on the server, so it keeps going with this page closed.';
  $('ft8BandRows').innerHTML = i.bands.map((b) => `<tr><td>${b.band}</td><td>${fmtHz(b.dial_hz)}</td>
    <td>${b.parked ? '<span class="dim">paused (VHF selected)</span>' : b.last_count == null ? '<span class="dim">waiting</span>' : `${b.last_count} at ${utc(b.last_slot)}`}</td>
    <td>${b.decodes}</td><td>${ft8.stations[b.band] ?? ''}</td>
    <td><button data-band="${b.band}">Listen</button></td></tr>`).join('');
}

function ft8Match(r, q) {
  if (!q) return true;
  return r.msg.includes(q);
}

function renderFt8Rows() {
  const q = $('ft8Find').value.trim().toUpperCase();
  const rows = ft8.rows.filter((r) => ft8Match(r, q));
  const newest = ft8.rows.length ? ft8.rows[0].t : 0;
  $('ft8Rows').innerHTML = rows.slice(0, 400).map((r) => `<tr class="${r.t === newest ? 'fresh' : ''}">
    <td>${utc(r.t)}</td><td>${r.band}</td><td>${r.score.toFixed(0)}</td><td>${r.dt.toFixed(1)}</td><td>${r.hz.toFixed(0)}</td>
    <td class="msg ${r.to_call === 'CQ' ? 'cq' : ''}">${esc(r.msg)}</td></tr>`).join('');
  $('ft8Count').textContent = `${rows.length} messages${q ? ' matching' : ''} (latest 1 h)`;
}

function renderFt8Qso(d) {
  const q = $('ft8Find').value.trim().toUpperCase();
  const convs = d.conversations.filter((c) => !q || c.a.includes(q) || c.b.includes(q) || c.messages.some((m) => m.msg.includes(q)));
  $('ft8Count').textContent = `${convs.length} conversations · ${d.cq.length} stations calling CQ (last 1 h)`;
  $('ft8Qso').innerHTML = convs.slice(0, 150).map((c) => `<div class="qso">
      <h4>${esc(c.a)} ↔ ${esc(c.b)} <span class="dim">${c.band} · ${c.messages.length} msgs · ${utc(c.first)}–${utc(c.last)} UTC</span>
        ${c.complete ? '<span class="done">✓ completed</span>' : ''}</h4>
      ${c.messages.map((m) => `<div>${utc(m.t)} ${String(m.score.toFixed(0)).padStart(3)}  ${esc(m.msg)}</div>`).join('')}
    </div>`).join('') || '<p class="dim">No conversations yet.</p>';
}

async function refreshFt8(reset) {
  try {
    ft8.info = await api('GET', '/api/ft8');
    const band = $('ft8Filter').value;
    if (reset === true || ft8.band !== band) { ft8.rows = []; ft8.lastId = 0; ft8.band = band; }
    if (ft8.view === 'msgs') {
      const since = Date.now() / 1000 - 3600;
      const add = await api('GET', `/api/ft8/decodes?since=${since}&after_id=${ft8.lastId}${band ? '&band=' + band : ''}&limit=2000`);
      if (add.length) {
        ft8.rows = add.concat(ft8.rows).sort((a, b) => b.t - a.t || a.band.localeCompare(b.band) || a.hz - b.hz);
        ft8.lastId = Math.max(ft8.lastId, ...add.map((r) => r.id));
      }
      const cut = Date.now() / 1000 - 3600;
      ft8.rows = ft8.rows.filter((r) => r.t > cut);
      renderFt8Rows();
    } else {
      renderFt8Qso(await api('GET', `/api/ft8/conversations?hours=1${band ? '&band=' + band : ''}`));
    }
    if (reset === true || !ft8.statsT || Date.now() - ft8.statsT > 60000) {
      const st = await api('GET', '/api/ft8/stats?hours=24');
      ft8.stations = Object.fromEntries(st.per_band.map((b) => [b.band, b.stations]));
      ft8.statsT = Date.now();
    }
    renderFt8Info();
  } catch (e) { console.warn('ft8', e); }
}

// ------------------------------------------------------------------ dashboard
async function refreshDashboard() {
  const band = $('dashBand').value, hours = $('dashHours').value;
  const [occ, tl, hh, ev] = await Promise.all([
    api('GET', `/api/occupancy/${band}?hours=${hours}`), api('GET', `/api/timeline/${band}?hours=${hours}`),
    api('GET', `/api/health/history?hours=${hours}`), api('GET', '/api/events?limit=30')]);
  $('dashInfo').textContent = `${occ.scans} scans of ${band} in the last ${hours} h`;
  drawOccupancy(occ, state.info.bands[band]);
  drawTimeline(tl, state.info.bands[band], hours);
  drawHealth(hh, hours);
  $('eventRows').innerHTML = ev.map((e) => `<tr><td>${fmtTime(e.t)}</td><td>${e.kind}</td><td class="dim">${e.detail}</td></tr>`).join('');
}

function prepCanvas(c) {
  c.width = Math.floor(c.clientWidth * devicePixelRatio);
  c.height = Math.floor(parseInt(c.getAttribute('height')) * devicePixelRatio);
  const g = c.getContext('2d');
  g.fillStyle = '#05070a'; g.fillRect(0, 0, c.width, c.height);
  g.font = `${11 * devicePixelRatio}px -apple-system, sans-serif`;
  return g;
}

function bandAxis(g, W, H, b, pad) {
  const span = b.stop_hz - b.start_hz, step = niceStep(span / 8);
  g.fillStyle = '#5b6573'; g.strokeStyle = '#1b222b';
  for (let f = Math.ceil(b.start_hz / step) * step; f <= b.stop_hz; f += step) {
    const x = pad + (f - b.start_hz) / span * (W - pad);
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H - 14 * devicePixelRatio); g.stroke();
    g.fillText(f >= 1e6 ? (f / 1e6).toFixed(3) : (f / 1e3).toFixed(0), x + 2, H - 3);
  }
}

const snrColor = (s) => { const c = Math.max(0, Math.min(255, s * 6)) | 0; return `rgb(${LUT[c * 3]},${LUT[c * 3 + 1]},${LUT[c * 3 + 2]})`; };

function drawOccupancy(occ, b) {
  const c = $('occ'), g = prepCanvas(c), W = c.width, H = c.height, pad = 30 * devicePixelRatio, bottom = H - 16 * devicePixelRatio;
  bandAxis(g, W, H, b, pad);
  if (!occ.scans) { g.fillStyle = '#8b96a5'; g.fillText('No scans of this band yet — run one from the Scan tab.', pad + 10, 30); return; }
  const span = b.stop_hz - b.start_hz, bw = Math.max(2, (b.step_hz || span / 200) / span * (W - pad) * 0.8);
  for (const r of occ.channels) {
    const x = pad + (r.freq_hz - b.start_hz) / span * (W - pad), h = r.heard / occ.scans * (bottom - 10);
    g.fillStyle = snrColor(r.mean_snr);
    g.fillRect(x - bw / 2, bottom - h, bw, h);
  }
  g.fillStyle = '#5b6573'; g.fillText('100%', 2, 14 * devicePixelRatio); g.fillText('0', 2, bottom);
}

function drawTimeline(tl, b, hours) {
  const c = $('timeline'), g = prepCanvas(c), W = c.width, H = c.height, pad = 30 * devicePixelRatio, bottom = H - 16 * devicePixelRatio;
  bandAxis(g, W, H, b, pad);
  if (!tl.length) return;
  const span = b.stop_hz - b.start_hz, t1 = Date.now() / 1000, t0 = t1 - hours * 3600;
  for (const r of tl) {
    const x = pad + (r.freq_hz - b.start_hz) / span * (W - pad), y = (r.t - t0) / (t1 - t0) * bottom;
    g.fillStyle = snrColor(r.snr_db);
    g.fillRect(x - 1.5 * devicePixelRatio, y - 1.5 * devicePixelRatio, 3 * devicePixelRatio, 3 * devicePixelRatio);
  }
  g.fillStyle = '#5b6573'; g.fillText(`-${hours}h`, 2, 12 * devicePixelRatio); g.fillText('now', 2, bottom);
}

function drawHealth(hh, hours) {
  const c = $('healthChart'), g = prepCanvas(c), W = c.width, H = c.height;
  if (hh.length < 2) { g.fillStyle = '#8b96a5'; g.fillText('Collecting (one sample every 30 s)…', 10, 30); return; }
  const t1 = Date.now() / 1000, t0 = t1 - hours * 3600;
  const series = [['if_power_db', '#4cc2ff', -60, 0, 'IF power dBFS'], ['rf_gain_db', '#ffb84c', 0, 40, 'RF gain dB'],
                  ['spectrum_fps', '#4cd98a', 0, 15, 'spectrum fps']];
  series.forEach(([k, col, lo, hi, name], i) => {
    g.strokeStyle = col; g.lineWidth = devicePixelRatio; g.beginPath();
    hh.forEach((r, j) => {
      const x = (r.t - t0) / (t1 - t0) * W, y = H - ((r[k] ?? lo) - lo) / (hi - lo) * H;
      j ? g.lineTo(x, y) : g.moveTo(x, y);
    });
    g.stroke(); g.fillStyle = col; g.fillText(name, 8 + i * 120 * devicePixelRatio, 14 * devicePixelRatio);
  });
  const overs = hh.filter((r) => r.ad_over);
  if (overs.length) { g.fillStyle = '#ff6b6b'; g.fillText(`ADC overloads seen: ${overs.at(-1).ad_over}`, 8, 30 * devicePixelRatio); }
}

// ------------------------------------------------------------------ wiring
function wire() {
  $('freq').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { const f = parseFreq($('freq').value); f ? tune(f) : flash('frequency? e.g. 860, 7.2M, 14074k'); $('freq').blur(); }
  });
  $('mode').addEventListener('change', () => { tune(null, $('mode').value); $('step').value = defaultStep($('mode').value, current().freq_hz); });
  $('listen').addEventListener('click', () => {
    if (!state.listening && state.band === 'vhf' && state.sweeping) {   // the tuner is sweeping: go live first
      setView(current().freq_hz, 2 * state.info.vhf.window_hz / state.view.n);
    }
    state.listening ? stopAudio() : startAudio();
  });
  $('volume').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = parseFloat($('volume').value); });
  $('record').addEventListener('click', async () => {
    const ch = current(); if (!ch) return;
    try {
      if (ch.recording) { await api('DELETE', `/api/channels/${ch.ssrc}/record`); ch.recording = false; }
      else { await api('POST', `/api/channels/${ch.ssrc}/record`); ch.recording = true; }
      renderMeter(); refreshRecordings();
    } catch (e) { flash(e.message); }
  });
  $('zoom').addEventListener('change', () => setView(current()?.freq_hz ?? state.view.center, +$('zoom').value / state.view.n));
  $('bandHF').addEventListener('click', () => { state.switching = true; switchBand('hf').finally(() => { state.switching = false; }); });
  $('bandVHF').addEventListener('click', () => { state.switching = true; switchBand('vhf').finally(() => { state.switching = false; }); });
  $('coverage').addEventListener('change', async () => {
    const rate = +$('coverage').value;
    $('coverage').disabled = true; flash('restarting radiod at ' + rate / 1e6 + ' MS/s…');
    try {
      state.info = await api('POST', '/api/coverage', {adc_rate_hz: rate});
      buildZoom(); flash(`coverage 0–${Math.floor(state.info.max_freq_hz / 1e6)} MHz`);
    } catch (e) { flash(e.message); $('coverage').value = String(state.info.adc_rate_hz); }
    finally { $('coverage').disabled = false; }
  });
  $('hfGain').addEventListener('input', () => { state.gainDragging = true; $('hfGainVal').textContent = `${(+$('hfGain').value).toFixed(1)} dB`; });
  $('hfGain').addEventListener('change', async () => {
    state.gainDragging = false;
    const db = +$('hfGain').value;
    try { await api('POST', '/api/hf/gain', {gain_db: db}); state.info.hf_gain = {...state.info.hf_gain, db}; }
    catch (e) { flash(e.message); }
  });
  $('hfAuto').addEventListener('change', async () => {
    const db = $('hfAuto').checked ? null : +$('hfGain').value;
    try { await api('POST', '/api/hf/gain', {gain_db: db}); state.info.hf_gain = {...state.info.hf_gain, db}; renderHfGain(db); }
    catch (e) { flash(e.message); }
  });
  // Left/right arrows always step the tuning (Shift: 10 steps), except while typing a frequency.
  // Capture phase, so a focused dropdown or slider doesn't swallow them.
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const t = e.target;
    if (t && (t.id === 'freq' || t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && t.type === 'number'))) return;
    e.preventDefault(); e.stopPropagation();
    if (t && t.blur && t !== document.body) t.blur();
    stepTune(e.key === 'ArrowRight' ? 1 : -1, e.shiftKey ? 10 : 1);
  }, true);
  $('stepDown').addEventListener('click', (e) => stepTune(-1, e.shiftKey ? 10 : 1));
  $('stepUp').addEventListener('click', (e) => stepTune(1, e.shiftKey ? 10 : 1));
  $('vga').addEventListener('input', () => { $('vgaVal').textContent = $('vga').value; });
  $('vga').addEventListener('change', () => api('POST', '/api/vhf/gain', {vga: +$('vga').value}).catch((e) => flash(e.message)));
  $('center').addEventListener('click', () => setView(current().freq_hz, null));
  $('autoscale').addEventListener('change', () => {});
  wireScope(spec); wireScope(wf);

  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('.tabs button, .tab').forEach((x) => x.classList.remove('active'));
    b.classList.add('active'); $('tab-' + b.dataset.tab).classList.add('active');
    if (b.dataset.tab === 'recordings') refreshRecordings();
    clearInterval(monitorTimer);
    if (b.dataset.tab === 'monitor') { refreshMonitor(); monitorTimer = setInterval(refreshMonitor, 2000); }
    if (b.dataset.tab === 'dashboard') refreshDashboard();
    clearInterval(ft8Timer); clearInterval(sstvTimer);
    if (b.dataset.tab === 'sstv') { refreshSstv(); sstvTimer = setInterval(refreshSstv, 3000); }
    if (b.dataset.tab === 'ft8') { refreshFt8(true); ft8Timer = setInterval(refreshFt8, 3000); }
  }));
  wireFt8();
  wireSstv();

  $('addChannel').addEventListener('click', async () => {
    const ch = current();
    const c = await api('POST', '/api/channels', {freq_hz: ch.freq_hz, mode: ch.mode, label: '', band: state.band});
    state.channels.push(c); renderChannels();
  });
  $('channelRows').addEventListener('click', async (e) => {
    const b = e.target.closest('button'); if (!b) return;
    const ssrc = +b.dataset.ssrc;
    if (b.dataset.act === 'sel') select(ssrc);
    if (b.dataset.act === 'del') {
      if (ssrc === state.selected && state.channels.length === 1) return flash('keep at least one channel');
      await api('DELETE', `/api/channels/${ssrc}`);
      state.channels = state.channels.filter((c) => c.ssrc !== ssrc);
      if (ssrc === state.selected) select(state.channels[0].ssrc);
      renderChannels();
    }
  });
  $('scanBtn').addEventListener('click', runScan);
  $('autoScan').addEventListener('change', () => {
    clearInterval(autoScanTimer);
    if ($('autoScan').checked) { runScan(); autoScanTimer = setInterval(runScan, Math.max(1, +$('autoMin').value) * 60000); }
  });
  $('scanRows').addEventListener('click', async (e) => {
    const b = e.target.closest('button'); if (!b) return;
    const f = +b.dataset.f, m = b.dataset.m;
    if (b.dataset.act === 'tune') { $('mode').value = m; tune(f, m); }
    if (b.dataset.act === 'add') { const c = await api('POST', '/api/channels', {freq_hz: f, mode: m, band: state.band}); state.channels.push(c); renderChannels(); }
  });
  $('schedAdd').addEventListener('click', async () => {
    try { await api('POST', '/api/schedules', {band: $('schedBand').value, every_min: +$('schedMin').value}); refreshMonitor(); }
    catch (e) { flash(e.message); }
  });
  $('watchAdd').addEventListener('click', async () => {
    const ch = current();
    try {
      await api('POST', '/api/watches', {freq_hz: ch.freq_hz, mode: ch.mode, label: '', snr_db: +$('watchSnr').value,
                                          transcribe: $('watchTx').checked});
      refreshMonitor();
    } catch (e) { flash(e.message); }
  });
  for (const id of ['schedRows', 'watchRows']) $(id).addEventListener('click', async (e) => {
    const b = e.target.closest('button'); if (!b) return;
    await api('DELETE', b.dataset.act === 'delsched' ? `/api/schedules/${b.dataset.id}` : `/api/watches/${b.dataset.id}`);
    refreshMonitor();
  });
  $('dashRefresh').addEventListener('click', refreshDashboard);
  $('dashBand').addEventListener('change', refreshDashboard);
  $('dashHours').addEventListener('change', refreshDashboard);
}

init().catch((e) => { $('health').innerHTML = `<span class="bad">startup failed: ${e.message}</span>`; });
