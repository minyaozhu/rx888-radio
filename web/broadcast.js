// Broadcast Radio: Bay Area AM/FM stations and scheduled shortwave broadcasters; scan, then click to listen.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p, opts) => {
  const r = await fetch(p, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const post = (p, b) => api(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const khz = (f) => (f / 1e3).toLocaleString(undefined, {maximumFractionDigits: 1});
const mhzs = (f) => (f / 1e6).toFixed(1);

const S = {sec: 'am', st: null, sw: null, hits: {am: {}, fm: {}, sw: {}}, scanned: {}, playing: null, chans: {}};

// ------------------------------------------------------------------ receiver input
// AM and shortwave need the HF input; FM the VHF tuner at the station's frequency. Either way the voice
// session (airband / public-safety rotation) is held so it doesn't switch the input away mid-listen.
async function ensureInput(sec, freq) {
  const h = await api('/api/health');
  if (sec === 'fm') {
    await post('/api/voice/focus', {focus: 'pause'});
    if (h.band !== 'vhf' || !h.vhf || Math.abs((h.vhf.center_hz || 0) - freq) > 3.5e6) await post('/api/band', {band: 'vhf', center_hz: freq});
    note('FM: the receiver is on the VHF tuner and the voice-session rotation is paused.');
  } else {
    const f = await api('/api/voice/focus');
    if (f.focus !== 'hf') await post('/api/voice/focus', {focus: 'hf'});
    if (h.band !== 'hf') { await post('/api/band', {band: 'hf'}); await new Promise((r) => setTimeout(r, 1500)); }
    note('AM / shortwave: the receiver is held on the HF input (FT8, WSPR and SSTV run too); the VHF rotation is paused.');
  }
}
function note(t) { $('noticeText').textContent = t; $('notice').style.display = ''; }
$('resume').addEventListener('click', async () => {
  stop();
  await post('/api/voice/focus', {focus: null});
  $('notice').style.display = 'none';
});

// ------------------------------------------------------------------ player
const audio = {ctx: null, gain: null, ws: null, next: 0};
async function play(item) {
  const sec = item.sec, freq = item.freq_hz, band = sec === 'fm' ? 'vhf' : 'hf', mode = sec === 'fm' ? 'wfm' : 'am';
  $('nowName').textContent = 'Tuning…'; $('nowMeta').textContent = '';
  try {
    await ensureInput(sec, freq);
    let ssrc = S.chans[band];
    if (ssrc) {
      try { await api(`/api/channels/${ssrc}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({freq_hz: freq, mode})}); }
      catch (e) { ssrc = null; }
    }
    if (!ssrc) { ssrc = (await post('/api/channels', {freq_hz: freq, mode, band, label: 'broadcast'})).ssrc; S.chans[band] = ssrc; }
    if (!audio.ctx) { audio.ctx = new AudioContext(); audio.gain = audio.ctx.createGain(); audio.gain.connect(audio.ctx.destination); }
    audio.ctx.resume(); audio.gain.gain.value = +$('vol').value;
    connect(ssrc);
    S.playing = {sec, freq};
    $('nowName').textContent = item.title;
    $('nowMeta').textContent = `${sec === 'fm' ? mhzs(freq) + ' MHz' : khz(freq) + ' kHz'} · ${item.sub || ''}`;
    $('stop').disabled = false;
  } catch (e) { $('nowName').textContent = 'Could not tune'; $('nowMeta').textContent = e.message; }
  draw();
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
    if (audio.next < now + 0.05 || audio.next > now + 1.0) audio.next = now + 0.25;
    src.start(audio.next); audio.next += buf.duration;
  };
  ws.onclose = () => { if (audio.ws === ws && S.playing) setTimeout(() => S.playing && connect(ssrc), 1000); };
  audio.ws = ws;
}
function stop() {
  connect(null); S.playing = null;
  $('nowName').textContent = 'Nothing playing'; $('nowMeta').textContent = 'pick a station below'; $('stop').disabled = true;
  draw();
}
$('stop').addEventListener('click', stop);
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });

// ------------------------------------------------------------------ scanning
const SW_BANDS = () => (S.sw ? S.sw.bands : []);
function selectedBands() { return [...document.querySelectorAll('#bands input:checked')].map((x) => x.value); }
$('scan').addEventListener('click', async () => {
  $('scan').disabled = true;
  try {
    if (S.sec === 'sw') {
      const bands = selectedBands().length ? selectedBands() : SW_BANDS().map((b) => b.name);
      await ensureInput('sw');
      for (const [k, b] of bands.entries()) {
        $('scanNote').textContent = `scanning ${b} (${k + 1}/${bands.length})…`;
        const r = await post(`/api/scan/${b}`, {});
        for (const h of r.hits) S.hits.sw[Math.round(h.freq_hz / 5000) * 5000] = h.snr_db;
      }
    } else {
      await ensureInput(S.sec, 98e6);
      $('scanNote').textContent = S.sec === 'fm' ? 'scanning 88–108 MHz (the tuner steps across the band, ~25 s)…' : 'scanning 530–1700 kHz…';
      const r = await post(`/api/scan/${S.sec}`, {});
      S.hits[S.sec] = {};
      for (const h of r.hits) S.hits[S.sec][Math.round(h.freq_hz)] = h.snr_db;
    }
    S.scanned[S.sec] = Date.now();
    $('scanNote').textContent = `scanned ${new Date().toLocaleTimeString([], {hour12: false})}`;
  } catch (e) { $('scanNote').textContent = 'scan failed: ' + e.message; }
  $('scan').disabled = false;
  draw();
});

// ------------------------------------------------------------------ tables
function sigCell(snr) {
  if (snr == null) return '<span class="lanesub">–</span>';
  return `<span class="sig"><b style="width:${Math.max(4, Math.min(100, snr * 2))}%"></b></span>${snr.toFixed(0)} dB`;
}
function hitFor(sec, f) {
  const H = S.hits[sec];
  if (sec === 'sw') return H[Math.round(f / 5000) * 5000];
  const tol = sec === 'fm' ? 60e3 : 2e3;
  let best = null;
  for (const [k, v] of Object.entries(H)) if (Math.abs(+k - f) <= tol && (best == null || v > best)) best = v;
  return best;
}
function rowsFor() {
  const q = $('find').value.trim().toLowerCase(), only = $('heardOnly').checked;
  const rows = [];
  if (S.sec === 'am' || S.sec === 'fm') {
    const list = S.st ? S.st[S.sec] : [];
    for (const s of list) rows.push({sec: S.sec, freq_hz: s.freq_hz, title: s.call, sub: `${s.city} · ${s.format}`, cols: [s.call, s.city, s.format], snr: hitFor(S.sec, s.freq_hz)});
    const known = new Set(list.map((s) => Math.round(s.freq_hz / (S.sec === 'fm' ? 1e5 : 1e4))));
    for (const [f, snr] of Object.entries(S.hits[S.sec])) {        // heard but not in the list
      if (!known.has(Math.round(+f / (S.sec === 'fm' ? 1e5 : 1e4)))) rows.push({sec: S.sec, freq_hz: +f, title: 'Unlisted station', sub: 'not in the Bay Area list', cols: ['?', '', 'heard, not in the list'], snr});
    }
  } else {
    const bands = selectedBands();
    const sched = (S.sw ? S.sw.now : []).filter((x) => !bands.length || bands.includes(x.band));
    const byF = new Map();
    for (const x of sched) {
      const k = Math.round(x.khz * 1000 / 5000) * 5000;
      if (!byF.has(k)) byF.set(k, []);
      byF.get(k).push(x);
    }
    for (const [f, xs] of byF) {
      const names = [...new Set(xs.map((x) => x.station))];
      rows.push({sec: 'sw', freq_hz: f, title: names.join(' / '), sub: `${[...new Set(xs.map((x) => x.language))].join(', ')} → ${[...new Set(xs.map((x) => x.target_name))].join(', ')}`,
        cols: [names.join(' / '), [...new Set(xs.map((x) => x.language))].join(', '), [...new Set(xs.map((x) => x.target_name))].join(', '), xs.map((x) => x.time).join(', '), xs[0].itu, xs[0].band],
        snr: hitFor('sw', f)});
    }
    for (const [f, snr] of Object.entries(S.hits.sw)) {
      if (byF.has(+f)) continue;
      const b = SW_BANDS().find((x) => +f >= x.start_hz && +f <= x.stop_hz);
      if (!b || (bands.length && !bands.includes(b.name))) continue;
      rows.push({sec: 'sw', freq_hz: +f, title: 'Unscheduled signal', sub: 'not in EiBi for this hour', cols: ['unscheduled signal', '', '', '', '', b.name], snr});
    }
  }
  return rows.filter((r) => (!only || r.snr != null) && (!q || (r.title + ' ' + r.sub).toLowerCase().includes(q)))
    .sort((a, b) => a.freq_hz - b.freq_hz);
}
function draw() {
  const sw = S.sec === 'sw';
  $('thead').innerHTML = sw ? '<tr><th>kHz</th><th>Station (EiBi, now)</th><th>Language</th><th>Target</th><th>UTC</th><th>From</th><th>Band</th><th>Signal</th><th></th></tr>'
    : `<tr><th>${S.sec === 'fm' ? 'MHz' : 'kHz'}</th><th>Station</th><th>City</th><th>Format</th><th>Signal</th><th></th></tr>`;
  const rows = rowsFor();
  $('rows').innerHTML = rows.map((r, i) => {
    const playing = S.playing && S.playing.sec === r.sec && Math.abs(S.playing.freq - r.freq_hz) < 1;
    const f = r.sec === 'fm' ? mhzs(r.freq_hz) : khz(r.freq_hz);
    return `<tr data-i="${i}" class="${playing ? 'playing' : ''} ${S.scanned[S.sec] && r.snr == null ? 'dim' : ''}"><td>${f}</td>
      ${r.cols.map((c, k) => `<td class="${k === 0 ? 'name' : ''}">${esc(c)}</td>`).join('')}<td>${sigCell(r.snr)}</td>
      <td><button class="smallbtn" data-i="${i}">${playing ? '● playing' : 'Listen'}</button></td></tr>`;
  }).join('') || `<tr><td colspan="9" class="lanesub">${sw ? 'Nothing scheduled in these bands right now.' : 'No stations.'}</td></tr>`;
  S.rows = rows;
  $('bands').style.display = sw ? '' : 'none';
}
$('rows').addEventListener('click', (e) => { const b = e.target.closest('button[data-i]'); if (b) play(S.rows[+b.dataset.i]); });
document.querySelectorAll('[data-sec]').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('[data-sec]').forEach((x) => x.classList.toggle('on', x === b));
  S.sec = b.dataset.sec;
  $('scanNote').textContent = S.scanned[S.sec] ? `scanned ${new Date(S.scanned[S.sec]).toLocaleTimeString([], {hour12: false})}` : 'not scanned yet';
  draw();
}));
$('heardOnly').addEventListener('change', draw);
$('find').addEventListener('input', draw);

async function load() {
  S.st = await api('/api/broadcast/stations');
  try {
    S.sw = await api('/api/broadcast/sw/now');
    $('bands').innerHTML = S.sw.bands.map((b) => `<label><input type="checkbox" value="${b.name}">${b.name}</label>`).join('')
      + `<span class="lanesub" style="align-self:center">schedule: ${esc(S.sw.file || 'none')} · ${S.sw.now.length} broadcasts scheduled now</span>`;
    $('bands').addEventListener('change', draw);
  } catch (e) { $('bands').innerHTML = `<span class="lanesub">no shortwave schedule: ${esc(e.message)}</span>`; }
  $('scanNote').textContent = 'not scanned yet';
  draw();
}
load();
setInterval(async () => {                 // the shortwave schedule changes on the hour and half hour
  try { S.sw = await api('/api/broadcast/sw/now'); if (S.sec === 'sw') draw(); } catch (e) { /* ignore */ }
}, 5 * 60000);
