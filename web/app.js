// SPDX-License-Identifier: GPL-3.0-or-later
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
  state.channels = await api('GET', '/api/channels');
  await applyBand(state.info.band);
  wire();
  initPanels();
  openStatus();
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

function renderHfGain(db) {
  const auto = db === null || db === undefined;
  $('hfAuto').checked = auto;
  $('hfGain').disabled = auto;
  if (!auto) $('hfGain').value = db;
  $('hfGainVal').textContent = auto ? 'auto' : `${(+$('hfGain').value).toFixed(1)} dB`;
}

function select(ssrc, ch) {
  state.selected = ssrc;
  ch = ch || state.channels.find((c) => c.ssrc === ssrc);
  if (!ch) return;
  $('freq').value = fmtTune(ch.freq_hz);
  $('mode').value = ch.mode;
  $('step').value = defaultStep(ch.mode, ch.freq_hz);
  if (state.listening) startAudio();
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
  }
  if (mode) ch.mode = mode;
  panelsRedraw();
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

let flashTimer = null;
function flash(msg) {
  $('flash').textContent = msg;
  clearTimeout(flashTimer); flashTimer = setTimeout(() => { $('flash').textContent = ''; }, 8000);
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
    // the selected channel may be retuned elsewhere (another page, the voice session): keep the box in step
    const now = current();
    if (now && document.activeElement !== $('freq') && !pendingTune && !tuneTimer) {
      const txt = fmtTune(now.freq_hz);
      if ($('freq').value !== txt) { $('freq').value = txt; panelsRedraw(); }
    }
    renderHealth(s.health, s.scanning);
    renderMeter();
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

// ------------------------------------------------------------------ wiring
function wire() {
  $('freq').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { const f = parseFreq($('freq').value); f ? tune(f) : flash('frequency? e.g. 860, 7.2M, 14074k'); $('freq').blur(); }
  });
  $('mode').addEventListener('change', () => { tune(null, $('mode').value); $('step').value = defaultStep($('mode').value, current().freq_hz); });
  $('listen').addEventListener('click', () => {
    state.listening ? stopAudio() : startAudio();
  });
  $('volume').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = parseFloat($('volume').value); });
  $('record').addEventListener('click', async () => {
    const ch = current(); if (!ch) return;
    try {
      if (ch.recording) { await api('DELETE', `/api/channels/${ch.ssrc}/record`); ch.recording = false; }
      else { await api('POST', `/api/channels/${ch.ssrc}/record`); ch.recording = true; }
      renderMeter();
    } catch (e) { flash(e.message); }
  });
  $('bandHF').addEventListener('click', () => { state.switching = true; switchBand('hf').finally(() => { state.switching = false; }); });
  $('bandVHF').addEventListener('click', () => { state.switching = true; switchBand('vhf').finally(() => { state.switching = false; }); });
  $('coverage').addEventListener('change', async () => {
    const rate = +$('coverage').value;
    $('coverage').disabled = true; flash('restarting radiod at ' + rate / 1e6 + ' MS/s…');
    try {
      state.info = await api('POST', '/api/coverage', {adc_rate_hz: rate});
      flash(`coverage 0–${Math.floor(state.info.max_freq_hz / 1e6)} MHz`);
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
    let shell = null; try { shell = window.parent !== window && window.parent.rxCtrl; } catch (err) { /* not ours */ }
    if (shell && shell.bindings[(e.shiftKey ? 'Shift+' : '') + e.code]) return;   // the tab shell's controller has the arrows
    const t = e.target;
    if (t && (t.id === 'freq' || t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && t.type === 'number'))) return;
    e.preventDefault(); e.stopPropagation();
    if (t && t.blur && t !== document.body) t.blur();
    if (e.shiftKey) { panelZoomKey(e.key === 'ArrowRight' ? 1 : -1); return; }
    stepTune(e.key === 'ArrowRight' ? 1 : -1, e.altKey ? 10 : 1);
  }, true);
  $('stepDown').addEventListener('click', (e) => stepTune(-1, e.shiftKey ? 10 : 1));
  $('stepUp').addEventListener('click', (e) => stepTune(1, e.shiftKey ? 10 : 1));
  $('vga').addEventListener('input', () => { $('vgaVal').textContent = $('vga').value; });
  $('vga').addEventListener('change', () => api('POST', '/api/vhf/gain', {vga: +$('vga').value}).catch((e) => flash(e.message)));
  $('autoscale').addEventListener('change', () => {});
}

// ------------------------------------------------------------------ remote control (controller.js in the tab shell)
// One action from a key, the Ulanzi dial or any other controller; returns a line for the on-screen display.
function cycleSelect(sel, dir) {
  const i = Math.max(0, Math.min(sel.options.length - 1, sel.selectedIndex + dir));
  if (i === sel.selectedIndex) return false;
  sel.selectedIndex = i; sel.dispatchEvent(new Event('change')); return true;
}
window.rxControl = (action) => {
  const ch = current();
  const freqText = () => ch ? fmtHz(ch.freq_hz) + ' ' + ch.mode.toUpperCase() : '';
  switch (action) {
    case 'tune_up': stepTune(1); return freqText();
    case 'tune_down': stepTune(-1); return freqText();
    case 'zoom_in': return panelZoomKey(1);
    case 'zoom_out': return panelZoomKey(-1);
    case 'tune_up_fast': stepTune(1, 10); return freqText();
    case 'tune_down_fast': stepTune(-1, 10); return freqText();
    case 'step_up': cycleSelect($('step'), 1); return 'step ' + $('step').selectedOptions[0].text;
    case 'step_down': cycleSelect($('step'), -1); return 'step ' + $('step').selectedOptions[0].text;
    case 'step_cycle': {
      const sel = $('step'); sel.selectedIndex = (sel.selectedIndex + 1) % sel.options.length; sel.dispatchEvent(new Event('change'));
      return 'step ' + sel.selectedOptions[0].text;
    }
    case 'vol_up': case 'vol_down': {
      const v = Math.max(0, Math.min(1, +$('volume').value + (action === 'vol_up' ? 0.05 : -0.05)));
      $('volume').value = v; $('volume').dispatchEvent(new Event('input'));
      return `volume ${Math.round(v * 100)}%${state.listening ? '' : ' (not listening)'}`;
    }
    case 'listen': $('listen').click(); return state.listening ? '▶ listening · ' + freqText() : '■ stopped';
    case 'mode_next': case 'mode_prev': {
      const sel = $('mode'), n = sel.options.length;
      sel.selectedIndex = (sel.selectedIndex + (action === 'mode_next' ? 1 : n - 1)) % n;
      sel.dispatchEvent(new Event('change')); return 'mode ' + sel.value.toUpperCase();
    }
    case 'band': (state.band === 'hf' ? $('bandVHF') : $('bandHF')).click(); return 'switching to ' + (state.band === 'hf' ? 'VHF' : 'HF');
    case 'chan_next': case 'chan_prev': {
      const list = state.channels.filter((c) => c.band === state.band && c.kind === 'listen');
      if (list.length < 2) return 'only one channel';
      const i = list.findIndex((c) => c.ssrc === state.selected);
      const next = list[(i + (action === 'chan_next' ? 1 : list.length - 1)) % list.length];
      select(next.ssrc, next); return `channel ${list.indexOf(next) + 1}/${list.length} · ${fmtHz(next.freq_hz)}`;
    }
    case 'record': $('record').click(); return ch && !ch.recording ? '● recording' : 'recording stopped';
  }
  return null;
};

init().catch((e) => { $('health').innerHTML = `<span class="bad">startup failed: ${e.message}</span>`; });
