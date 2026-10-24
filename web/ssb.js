// SPDX-License-Identifier: GPL-3.0-or-later
// SSB Voice: known HF voice channels (amateur, aviation, military, marine) and amateur-band activity;
// scan, then click to listen in USB / LSB and nudge the tuning.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p, opts) => {
  const r = await fetch(p, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const post = (p, b) => api(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
const patch = (p, b) => api(p, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const khz = (f) => (f / 1e3).toLocaleString(undefined, {minimumFractionDigits: 1, maximumFractionDigits: 1});
const hms = (t) => new Date(t).toLocaleTimeString([], {hour12: false});

const S = {sec: 'ham', data: null, snr: {}, activity: [], scanned: {}, playing: null, ssrc: null, rows: []};

// ------------------------------------------------------------------ receiver input
async function ensureHF() {
  const [h, f] = await Promise.all([api('/api/health'), api('/api/voice/focus')]);
  if (f.focus !== 'hf') await post('/api/voice/focus', {focus: 'hf'});
  if (h.band !== 'hf') { await post('/api/band', {band: 'hf'}); await new Promise((r) => setTimeout(r, 1500)); }
  $('noticeText').textContent = 'The receiver is held on the HF input (FT8, WSPR and SSTV keep running; the VHF rotation is paused).';
  $('notice').style.display = '';
}
$('resume').addEventListener('click', async () => {
  stop();
  await post('/api/voice/focus', {focus: null});
  $('notice').style.display = 'none';
});

// ------------------------------------------------------------------ player
const audio = {ctx: null, gain: null, ws: null, next: 0};
async function play(row) {
  $('nowName').textContent = 'Tuning…'; $('nowMeta').textContent = '';
  try {
    await ensureHF();
    await tuneTo(row.freq_hz, row.mode);
    if (!audio.ctx) { audio.ctx = new AudioContext(); audio.gain = audio.ctx.createGain(); audio.gain.connect(audio.ctx.destination); }
    audio.ctx.resume(); audio.gain.gain.value = +$('vol').value;
    connect(S.ssrc);
    S.playing = {key: row.key, freq: row.freq_hz, mode: row.mode, title: row.title, sub: row.sub};
    $('stop').disabled = false; $('tune').style.visibility = '';
    showNow();
  } catch (e) { $('nowName').textContent = 'Could not tune'; $('nowMeta').textContent = e.message; }
  draw();
}
async function tuneTo(freq, mode) {
  if (S.ssrc) {
    try { await patch(`/api/channels/${S.ssrc}`, {freq_hz: freq, mode}); return; } catch (e) { S.ssrc = null; }
  }
  S.ssrc = (await post('/api/channels', {freq_hz: freq, mode, band: 'hf', label: 'ssb'})).ssrc;
}
function showNow() {
  const p = S.playing; if (!p) return;
  $('nowName').textContent = p.title;
  $('nowMeta').textContent = p.sub || '';
  $('nowFreq').textContent = khz(p.freq) + ' kHz';
  $('side').textContent = p.mode.toUpperCase();
}
let nudgeTimer = null;
function retune() {                      // coalesce quick nudges into one request
  clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(() => S.playing && tuneTo(S.playing.freq, S.playing.mode).catch((e) => { $('nowMeta').textContent = e.message; }), 120);
}
document.querySelectorAll('[data-nudge]').forEach((b) => b.addEventListener('click', () => {
  if (!S.playing) return;
  S.playing.freq += +b.dataset.nudge; showNow(); retune();
}));
$('side').addEventListener('click', () => {
  if (!S.playing) return;
  S.playing.mode = S.playing.mode === 'usb' ? 'lsb' : 'usb'; showNow(); retune();
});
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
  $('nowName').textContent = 'Nothing playing'; $('nowMeta').textContent = 'pick a channel below';
  $('stop').disabled = true; $('tune').style.visibility = 'hidden';
  draw();
}
$('stop').addEventListener('click', stop);
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });

// ------------------------------------------------------------------ scanning
const selectedBands = () => [...document.querySelectorAll('#bands input:checked')].map((x) => x.value);
$('scan').addEventListener('click', async () => {
  $('scan').disabled = true;
  try {
    await ensureHF();
    if (S.sec === 'ham') {
      const all = S.data.ham_bands.map((b) => b.band);
      const bands = (selectedBands().length ? selectedBands() : all).filter((b) => all.includes(b));   // 60 m: channels only
      const found = [];
      for (const [k, b] of bands.entries()) {
        $('scanNote').textContent = `searching ${S.data.ham_bands.find((x) => x.band === b).label} for conversations (${k + 1}/${bands.length})…`;
        try {
          const r = await post(`/api/scan/${b}?min_snr=8`, {});
          const side = r.mode;
          for (const h of r.hits) {
            // speech energy peaks a few hundred Hz into the voice band: put the dial ~600 Hz the other way
            const dial = Math.round((h.freq_hz + (side === 'usb' ? -600 : 600)) / 100) * 100;
            found.push({band: b, freq_hz: dial, mode: side, snr: h.snr_db});
          }
        } catch (e) { /* a band beyond the coverage */ }
      }
      S.activity = found;
    }
    $('scanNote').textContent = 'checking the listed channels…';
    const r = await post('/api/ssb/check', {group: S.sec});
    for (const x of r.results) S.snr[x.freq_hz] = x.snr_db;
    S.scanned[S.sec] = Date.now();
    $('scanNote').textContent = `scanned ${hms(S.scanned[S.sec])}` + (S.sec === 'ham' ? ` · ${S.activity.length} conversations found` : '');
  } catch (e) { $('scanNote').textContent = 'scan failed: ' + e.message; }
  $('scan').disabled = false;
  draw();
});

// ------------------------------------------------------------------ table
const HEARD = 6;                            // dB above the floor that counts as a signal
function sigCell(snr) {
  if (snr == null) return '<span class="lanesub">–</span>';
  return `<span class="sig"><b style="width:${Math.max(4, Math.min(100, snr * 3))}%"></b></span>${snr.toFixed(0)} dB`;
}
function rowsFor() {
  const q = $('find').value.trim().toLowerCase(), only = $('heardOnly').checked;
  const bandOf = (f) => S.data.ham_bands.find((b) => {
    const r = {"160m": [1.8e6, 2e6], "80m": [3.6e6, 4e6], "40m": [7.125e6, 7.3e6], "20m": [14.15e6, 14.35e6], "17m": [18.11e6, 18.168e6],
      "15m": [21.2e6, 21.45e6], "12m": [24.93e6, 24.99e6], "10m": [28.3e6, 28.85e6]}[b.band];
    return r && f >= r[0] && f <= r[1];
  });
  const rows = S.data.channels.filter((c) => c.group === S.sec).map((c) => ({
    key: 'c' + c.freq_hz, freq_hz: c.freq_hz, mode: c.mode, title: c.name, sub: c.note,
    cols: [c.name, c.note], snr: S.snr[c.freq_hz] ?? null, off: !c.reachable}));
  if (S.sec === 'ham') {
    const bands = selectedBands();
    for (const a of S.activity) {
      if (bands.length && !bands.includes(a.band)) continue;
      if (rows.some((r) => Math.abs(r.freq_hz - a.freq_hz) < 2500)) continue;   // a listed channel already
      const b = S.data.ham_bands.find((x) => x.band === a.band);
      rows.push({key: 'a' + a.freq_hz, freq_hz: a.freq_hz, mode: a.mode, title: 'Activity', sub: `${b.label} · conversation found by the scan`,
        cols: ['Activity', `${b.label} phone band · tuning is approximate`], snr: a.snr, activity: true});
    }
    if (bands.length) {
      return finish(rows.filter((r) => r.activity || bandOf(r.freq_hz) && bands.includes(bandOf(r.freq_hz).band) ||
        (r.freq_hz > 5.3e6 && r.freq_hz < 5.41e6 && bands.includes('60m'))), q, only);
    }
  }
  return finish(rows, q, only);
}
function finish(rows, q, only) {
  return rows.filter((r) => (!only || (r.snr != null && r.snr >= HEARD)) && (!q || (r.title + ' ' + r.sub + ' ' + khz(r.freq_hz)).toLowerCase().includes(q)))
    .sort((a, b) => a.freq_hz - b.freq_hz);
}
function draw() {
  if (!S.data) return;
  $('thead').innerHTML = '<tr><th>kHz</th><th>Mode</th><th>Channel</th><th>Use</th><th>Signal</th><th></th></tr>';
  const rows = rowsFor();
  $('rows').innerHTML = rows.map((r, i) => {
    const playing = S.playing && S.playing.key === r.key;
    const quiet = S.scanned[S.sec] && (r.snr == null || r.snr < HEARD);
    return `<tr data-i="${i}" class="${playing ? 'playing' : ''} ${quiet || r.off ? 'dim' : ''} ${r.activity ? 'activity' : ''}"><td>${khz(r.freq_hz)}</td>
      <td><span class="mode">${r.mode.toUpperCase()}</span></td>
      ${r.cols.map((c, k) => `<td class="${k === 0 ? 'name' : ''}">${esc(c)}</td>`).join('')}<td>${r.off ? '<span class="lanesub">beyond coverage</span>' : sigCell(r.snr)}</td>
      <td>${r.off ? '' : `<button class="smallbtn" data-i="${i}">${playing ? '● playing' : 'Listen'}</button>`}</td></tr>`;
  }).join('') || '<tr><td colspan="6" class="lanesub">No channels.</td></tr>';
  S.rows = rows;
  $('bands').style.display = S.sec === 'ham' ? '' : 'none';
}
$('rows').addEventListener('click', (e) => { const b = e.target.closest('button[data-i]'); if (b) play(S.rows[+b.dataset.i]); });
document.querySelectorAll('[data-sec]').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('[data-sec]').forEach((x) => x.classList.toggle('on', x === b));
  S.sec = b.dataset.sec;
  $('scanNote').textContent = S.scanned[S.sec] ? `scanned ${hms(S.scanned[S.sec])}` : 'not scanned yet';
  draw();
}));
$('heardOnly').addEventListener('change', draw);
$('find').addEventListener('input', draw);

async function load() {
  S.data = await api('/api/ssb/channels');
  $('bands').innerHTML = [...S.data.ham_bands, {band: '60m', label: '60 m channels'}]
    .map((b) => `<label><input type="checkbox" value="${b.band}">${b.label}</label>`).join('')
    + '<span class="lanesub" style="align-self:center">none ticked = every band</span>';
  $('bands').addEventListener('change', draw);
  $('scanNote').textContent = 'not scanned yet';
  draw();
}
load().catch((e) => { $('rows').innerHTML = `<tr><td colspan="6" class="lanesub">rxapp not reachable: ${esc(e.message)}</td></tr>`; });
