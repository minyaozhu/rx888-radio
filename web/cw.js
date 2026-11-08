// SPDX-License-Identifier: GPL-3.0-or-later
// CW Skimmer: every Morse signal the server-side skimmer (rxapp/cw.py) is decoding, its spots and its log;
// click a signal to listen to it in CW (radiod's cwu: a 400 Hz filter, heard as a 500 Hz tone).
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p, o) => { const r = await fetch(p, o); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); };
const post = (p, b) => api(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
const patch = (p, b) => api(p, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const khz = (f) => (f / 1e3).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const utc = (t) => new Date(t * 1000).toISOString().slice(11, 16);
const BANDS = ['160m', '80m', '40m', '30m', '20m', '17m', '15m', '12m', '10m', '6m'];
const CALL = /^(?:[A-Z0-9]{1,3}\/)?[A-Z0-9]{1,3}\d{1,2}[A-Z]{1,4}(?:\/[A-Z0-9]{1,4})?$/;

const S = {info: null, live: [], spots: [], log: [], band: '', sel: null, playing: null, ssrc: null, health: null};
try { S.band = localStorage.getItem('cw.band') || ''; } catch (e) { /* storage blocked */ }

// calls in decoded text, highlighted
const marked = (text) => esc(text).split(' ').map((w) => CALL.test(w) && /[A-Z]/.test(w.slice(0, 3)) ? `<span class="call">${w}</span>` : w).join(' ');

// ------------------------------------------------------------------ drawing
function drawBands() {
  const i = S.info; if (!i) return;
  const count = {};
  for (const s of S.live) count[s.band] = (count[s.band] || 0) + 1;
  const bands = i.running ? i.bands.map((b) => b.band) : i.available;
  if (S.band && !bands.includes(S.band)) S.band = '';
  const chip = (b, label, n) => `<button type="button" data-band="${b}" class="${S.band === b ? 'on' : ''}${n ? '' : ' idle'}" aria-pressed="${S.band === b}">${label}${n ? `<b>${n}</b>` : ''}</button>`;
  $('bands').innerHTML = chip('', 'All', S.live.length) + bands.map((b) => chip(b, b, count[b] || 0)).join('');
  const fb = i.bands.find((b) => b.band === S.band);
  $('bandRange').textContent = fb ? `${(fb.lo_hz / 1e6).toFixed(3)}–${(fb.hi_hz / 1e6).toFixed(3)} MHz` : '';
}
$('bands').addEventListener('click', (e) => {
  const b = e.target.closest('[data-band]'); if (!b) return;
  S.band = b.dataset.band;
  try { localStorage.setItem('cw.band', S.band); } catch (err) { /* storage blocked */ }
  drawBands(); drawSigs();
});

function drawSigs() {
  const now = Date.now() / 1000;
  const sigs = S.live.filter((s) => !S.band || s.band === S.band);
  if (!sigs.length) {
    const i = S.info;
    $('sigs').innerHTML = `<div class="empty">${!i || !i.running ? 'The skimmer is stopped.' : 'Nothing keyed right now' + (S.band ? ` on ${S.band}` : '') + '. Signals appear as soon as a station sends a few characters.'}</div>`;
    return;
  }
  $('sigs').innerHTML = sigs.map((s) => {
    const sel = S.playing && Math.abs(S.playing.freq - s.freq_hz) < 60;
    return `<div class="sig${sel ? ' sel' : ''}${now - s.last > 6 ? ' stale' : ''}" data-f="${s.freq_hz}" title="${esc(s.band)} · since ${utc(s.since)} UTC · click to listen">
      <i class="k${s.keyed ? ' on' : ''}"></i><span class="f">${khz(s.freq_hz)}</span><span class="w">${Math.round(s.wpm)}</span>
      <span class="s">${Math.round(s.snr)}</span><span class="x"><span>${marked(s.text.trimEnd())}</span></span></div>`;
  }).join('');
}
$('sigs').addEventListener('click', (e) => { const r = e.target.closest('[data-f]'); if (r) listen(+r.dataset.f); });

function drawSpots() {
  const cut = Date.now() / 1000 - 3600;
  const hour = S.spots.filter((s) => s.t > cut);
  $('fSpots').textContent = hour.length;
  $('fSt').textContent = new Set(hour.map((s) => s.call)).size;
  const rows = S.spots.filter((s) => !S.band || s.band === S.band).slice(0, 300);
  $('spots').innerHTML = rows.map((s) => { const p = typeof prefixInfo === 'function' ? prefixInfo(s.call) : null;
    return `<tr data-f="${s.freq_hz}"><td>${utc(s.t)}</td><td>${esc(s.band)}</td><td>${khz(s.freq_hz)}</td><td class="c">${esc(s.call)}</td>
      <td class="m">${esc(p ? p.name : '')}</td><td>${Math.round(s.wpm)}</td><td>${Math.round(s.snr)}</td></tr>`; }).join('')
    || `<tr><td colspan="7" class="m">No spots yet${S.band ? ' on ' + S.band : ''}.</td></tr>`;
  const log = S.log.filter((x) => !S.band || x.band === S.band).slice(0, 120);
  $('log').innerHTML = log.map((x) => `<li data-f="${x.freq_hz}"><span class="h">${utc(x.t)}–${utc(x.t_end)} ${esc(x.band)} ${khz(x.freq_hz)} · ${Math.round(x.wpm)} wpm</span>${marked(x.text)}</li>`).join('')
    || '<li class="m" style="cursor:default;color:var(--muted)">Transmissions land here when a station goes quiet.</li>';
}
$('spots').addEventListener('click', (e) => { const r = e.target.closest('[data-f]'); if (r) listen(+r.dataset.f); });
$('log').addEventListener('click', (e) => { const r = e.target.closest('[data-f]'); if (r) listen(+r.dataset.f); });

function drawMonitor() {
  const i = S.info; if (!i) return;
  const hearing = i.bands.filter((b) => !b.parked).length;
  $('fMon').className = 'live' + (i.running && hearing ? '' : ' off');
  $('fMon').lastChild.textContent = i.running ? `on · ${hearing}/${i.bands.length} bands hearing` : 'stopped';
  $('fNow').textContent = i.running ? `${S.live.length} signal${S.live.length === 1 ? '' : 's'}` : '–';
  $('monBtn').textContent = i.running ? 'Stop CW skimmer' : 'Start CW skimmer';
  const h = S.health;
  const notice = h && h.ok === false && h.fails ? 'The receiver isn’t answering (radiod can’t reach the RX-888: is it plugged in?). rxapp keeps retrying.'
    : !i.running ? `The CW skimmer is stopped. Start it to decode every CW signal on ${i.available.length} bands at once.`
    : i.bands.every((b) => b.parked) ? 'The receiver is on VHF right now, so the skimmer is paused; it decodes whenever the HF input is selected. “Switch receiver here” keeps it there.'
    : '';
  $('notice').textContent = notice; $('notice').style.display = notice ? '' : 'none';
}
$('monBtn').addEventListener('click', async () => {
  $('monBtn').disabled = true;
  try { S.info = await post('/api/cw', {on: !S.info.running}); } catch (e) { alert(e.message); }
  $('monBtn').disabled = false; drawMonitor(); drawBands(); drawSigs();
});

// ------------------------------------------------------------------ listening
// On HF already (the skimmer's input): just listen. On VHF: take the receiver over to HF.
async function ensureHF() {
  const [h, f] = await Promise.all([api('/api/health'), api('/api/voice/focus')]);
  if (h.band === 'hf') return;
  if (f.focus !== 'hf') await post('/api/voice/focus', {focus: 'hf'});
  await post('/api/band', {band: 'hf'});
  await new Promise((r) => setTimeout(r, 1500));
}
const audio = {ctx: null, gain: null, ws: null, next: 0};
async function listen(freq) {
  freq = Math.round(freq / 10) * 10;
  $('nowFreq').textContent = khz(freq) + ' kHz'; $('nowMeta').textContent = 'tuning…';
  try {
    await ensureHF();
    if (S.ssrc) {
      try { await patch(`/api/channels/${S.ssrc}`, {freq_hz: freq, mode: 'cwu'}); } catch (e) { S.ssrc = null; }
    }
    if (!S.ssrc) S.ssrc = (await post('/api/channels', {freq_hz: freq, mode: 'cwu', band: 'hf', label: 'cw listen'})).ssrc;
    if (!audio.ctx) { audio.ctx = new AudioContext(); audio.gain = audio.ctx.createGain(); audio.gain.connect(audio.ctx.destination); }
    audio.ctx.resume(); audio.gain.gain.value = +$('vol').value;
    if (!S.playing) connect(S.ssrc);
    audioFocus.claim(stop);
    S.playing = {freq};
    const s = S.live.find((x) => Math.abs(x.freq_hz - freq) < 60);
    $('nowMeta').textContent = s ? `${s.band} · ${Math.round(s.wpm)} wpm · CW (500 Hz tone)` : 'CW (500 Hz tone)';
    $('stop').disabled = false;
  } catch (e) { $('nowMeta').textContent = 'could not tune: ' + e.message; }
  drawSigs();
}
function connect(ssrc) {
  if (audio.ws) { const w = audio.ws; audio.ws = null; w.onmessage = w.onclose = null; w.close(); }
  if (!ssrc) return;
  audio.next = 0;
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + `/ws/audio/${ssrc}`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    const rate = new DataView(e.data).getUint32(0, true), pcm = new Int16Array(e.data, 4);
    if (!pcm.length) return;
    const buf = audio.ctx.createBuffer(1, pcm.length, rate), out = buf.getChannelData(0);
    for (let k = 0; k < pcm.length; k++) out[k] = pcm[k] / 32768;
    const src = audio.ctx.createBufferSource(); src.buffer = buf; src.connect(audio.gain);
    const now = audio.ctx.currentTime;
    if (audio.next < now + 0.05 || audio.next > now + 1.0) audio.next = now + 0.2;
    src.start(audio.next); audio.next += buf.duration;
  };
  ws.onclose = () => { if (audio.ws === ws && S.playing) setTimeout(() => S.playing && connect(ssrc), 1000); };
  audio.ws = ws;
}
function stop() {
  audioFocus.release();
  connect(null); S.playing = null;
  if (S.ssrc) { fetch(`/api/channels/${S.ssrc}`, {method: 'DELETE'}).catch(() => {}); S.ssrc = null; }
  $('nowFreq').textContent = '–'; $('nowMeta').textContent = 'click a signal'; $('stop').disabled = true;
  drawSigs();
}
$('stop').addEventListener('click', stop);
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });
addEventListener('pagehide', () => { if (S.ssrc) fetch(`/api/channels/${S.ssrc}`, {method: 'DELETE', keepalive: true}).catch(() => {}); });

// ------------------------------------------------------------------ polling
async function pollLive() {
  try { S.live = await api('/api/cw/live'); } catch (e) { S.live = []; }
  drawBands(); drawSigs(); drawMonitor();
}
async function pollInfo() {
  try { [S.info, S.health] = await Promise.all([api('/api/cw'), api('/api/health').catch(() => null)]); }
  catch (e) { $('notice').textContent = 'rxapp not reachable: ' + e.message; $('notice').style.display = ''; return; }
  drawMonitor(); drawBands();
}
async function pollSpots() {
  try {
    const since = Date.now() / 1000 - 24 * 3600;
    [S.spots, S.log] = await Promise.all([api(`/api/cw/spots?since=${since}&limit=3000`), api(`/api/cw/text?since=${since}&limit=300`)]);
  } catch (e) { return; }
  drawSpots();
}
pollInfo().then(pollLive); pollSpots();
setInterval(pollLive, 1000); setInterval(pollInfo, 5000); setInterval(pollSpots, 10000);
