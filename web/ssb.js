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
// On HF already and held by another page (FT8, AM / SW ...): just listen. On HF with the voice session
// rotating: hold it on HF. On VHF: take the receiver over.
async function ensureHF() {
  const [h, f] = await Promise.all([api('/api/health'), api('/api/voice/focus')]);
  if (h.band === 'hf' && f.focus) {
    $('noticeText').textContent = `Listening on the HF input; the receiver stays with ${f.label || f.focus}.`;
    $('notice').style.display = '';
    return;
  }
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
const audio = {ctx: null, gain: null, ws: null, next: 0, ssrc: null, retry: null};
async function play(row) {
  $('nowName').textContent = 'Tuning…'; $('nowMeta').textContent = '';
  try {
    await ensureHF();
    await tuneTo(row.freq_hz, row.mode);
    if (!audio.ctx) { audio.ctx = new AudioContext(); audio.gain = audio.ctx.createGain(); audio.gain.connect(audio.ctx.destination); }
    audio.ctx.resume(); audio.gain.gain.value = +$('vol').value;
    connect(S.ssrc);
    audioFocus.claim(stop);
    S.playing = {key: row.key, freq: row.freq_hz, mode: row.mode, title: row.title, sub: row.sub};
    setTimeout(beat, 0);                   // start transcribing now, not at the next heartbeat
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
  clearTimeout(audio.retry); audio.retry = null;      // a pending reconnect is for the stream being replaced
  if (audio.ws) { const w = audio.ws; audio.ws = null; w.onmessage = w.onclose = null; w.close(); }
  audio.ssrc = ssrc;
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
  // Server restart or network blip: reconnect while still listening, to the channel still wanted. The old
  // version reconnected to the ssrc it was opened with, and a timer left over from a dead channel replaced
  // the next station's stream about a second after it started. 4404: the channel is gone, make a new one.
  ws.onclose = (e) => {
    if (audio.ws !== ws || !S.playing) return;
    audio.ws = null;
    audio.retry = setTimeout(async () => {
      audio.retry = null;
      if (!S.playing || audio.ssrc !== ssrc) return;
      try { if (e.code === 4404) { S.ssrc = null; await tuneTo(S.playing.freq, S.playing.mode); } } catch (err) { /* rxapp still starting: try again */ }
      connect(e.code === 4404 ? S.ssrc : ssrc);
    }, 1000);
  };
  audio.ws = ws;
}
function stop() {
  audioFocus.release();
  connect(null); S.playing = null;
  $('nowName').textContent = 'Nothing playing'; $('nowMeta').textContent = 'pick a channel below';
  $('stop').disabled = true; $('tune').style.visibility = 'hidden';
  draw(); tStatus(null); autoNote(null);
}
$('stop').addEventListener('click', stop);
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });

// ------------------------------------------------------------------ scanning
const selectedBands = () => [...document.querySelectorAll('#bands input:checked')].map((x) => x.value);
// Results show up as they come in (postStream: ndjson.js): conversations band by band, then the listed
// channels one spectrum window (~430 kHz of channels) at a time.
$('scan').addEventListener('click', async () => {
  $('scan').disabled = true;
  const sec = S.sec;                       // the section being scanned, even if you switch while it runs
  const all = S.data.ham_bands.map((b) => b.band);
  const sel = sec === 'ham' ? (selectedBands().length ? selectedBands() : all) : [];
  S.scanning = {sec, bands: sel};          // the spectrum tile shows all of it while the scan runs
  const searched = [];                     // the bands whose search finished
  try {
    await ensureHF();
    if (sec === 'ham') {
      const bands = sel.filter((b) => all.includes(b));   // 60 m: channels only
      S.activity = [];
      draw();
      for (const [k, b] of bands.entries()) {
        $('scanNote').textContent = `searching ${S.data.ham_bands.find((x) => x.band === b).label} for conversations (${k + 1}/${bands.length})… ${S.activity.length} found so far`;
        try {
          const r = await postStream(`/api/scan/${b}/stream`, {}, () => {});
          searched.push(b);
          const side = r.mode;
          for (const h of r.hits) {
            // speech energy peaks a few hundred Hz into the voice band: put the dial ~600 Hz the other way
            const dial = Math.round((h.freq_hz + (side === 'usb' ? -600 : 600)) / 100) * 100;
            S.activity.push({band: b, freq_hz: dial, mode: side, snr: h.snr_db});
          }
          draw();
        } catch (e) { /* a band beyond the coverage */ }
      }
    }
    // listed channels inside a band just searched were covered by that search: only the rest are measured
    const skip = sec === 'ham' ? S.data.ham_bands.filter((h) => searched.includes(h.band) && h.lo_hz != null).map((h) => [h.lo_hz, h.hi_hz]) : [];
    // (their reading: a conversation the search found on them, else none)
    for (const c of S.data.channels) {
      if (c.group !== sec || !skip.some(([a, b]) => c.freq_hz >= a && c.freq_hz <= b)) continue;
      const hit = S.activity.filter((a) => Math.abs(a.freq_hz - c.freq_hz) < 2500).sort((x, y) => y.snr - x.snr)[0];
      if (hit) S.snr[c.freq_hz] = hit.snr; else delete S.snr[c.freq_hz];
    }
    $('scanNote').textContent = 'checking the listed channels…';
    let n = 0;
    const r = await postStream('/api/ssb/check/stream', {group: sec, skip}, (part) => {
      for (const x of part) S.snr[x.freq_hz] = x.snr_db;
      n += part.length;
      $('scanNote').textContent = `checking the listed channels… ${n} measured`;
      draw();
    });
    for (const x of r.results) S.snr[x.freq_hz] = x.snr_db;
    S.scanned[sec] = Date.now();
    $('scanNote').textContent = `scanned ${hms(S.scanned[sec])}` + (sec === 'ham' ? ` · ${S.activity.length} conversations found` : '');
  } catch (e) { $('scanNote').textContent = 'scan failed: ' + e.message; }
  S.scanning = null;
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
  const bandOf = (f) => S.data.ham_bands.find((b) => b.lo_hz != null && f >= b.lo_hz && f <= b.hi_hz);
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

// ------------------------------------------------------------------ transcript (rxapp/ssbvoice.py)
// While playing with Transcribe or Auto-tune on, a heartbeat tells rxapp which channel to listen to; it stops on
// its own some seconds after the heartbeats do (Stop, another channel's audio, a closed tab). Auto-tune retunes
// the channel in rxapp (rxapp/ssbtune.py); the heartbeat's answer says where to.
const T = {list: [], newest: 0, playing: null, moveSeen: 0};
const store = {get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }};
$('autoTune').checked = store.get('ssb.autotune') !== '0';
$('autoTune').addEventListener('change', () => { store.set('ssb.autotune', $('autoTune').checked ? '1' : '0'); beat(); });
async function beat() {
  const tOn = $('tOn').checked, aOn = $('autoTune').checked;
  if (!S.playing || !S.ssrc || !(tOn || aOn)) { autoNote(null); return tStatus(null); }
  try {
    const st = await post('/api/ssb/transcribe/beat', {ssrc: S.ssrc, transcribe: tOn, autotune: aOn});
    followAuto(st); tStatus(st);
  } catch (e) { $('tStatus').textContent = e.message; }
}
function followAuto(st) {
  const a = st && st.autotune, last = a && a.last;
  if (S.playing && last && last.moved && last.t > T.moveSeen && st.ssrc === S.ssrc) {
    T.moveSeen = last.t;
    if (Math.abs(st.freq_hz - S.playing.freq) >= 1) { S.playing.freq = st.freq_hz; showNow(); }
  }
  autoNote(st);
}
function autoNote(st) {
  const a = st && st.autotune;
  if (!S.playing || !$('autoTune').checked || !a || !a.on || !st.listening) { $('autoNote').textContent = ''; return; }
  const l = a.last, sgn = (x) => (x > 0 ? '+' : x < 0 ? '−' : '±') + Math.abs(x).toFixed(0);
  $('autoNote').textContent = !l ? `auto: listening for a voice (${Math.min(a.frames, a.need)}/${a.need})`
    : l.moved ? `auto: voice ${sgn(l.offset_hz)} Hz → ${khz(l.to_hz)}${l.snapped ? '' : ` (${(l.to_hz / 1e3).toFixed(2)})`}`
    : l.waiting ? `auto: ${sgn(l.offset_hz)} Hz, checking with the next voice`
    : `auto: in tune (${sgn(l.offset_hz)} Hz)`;
  $('autoNote').title = l ? `last estimate ${new Date(l.t * 1000).toLocaleTimeString([], {hour12: false})} from ${l.frames} voiced 40 ms frames` : '';
}
function tStatus(st) {
  const on = $('tOn').checked;
  $('tStatus').textContent = !on ? 'off' : !S.playing ? 'play a channel to transcribe it'
    : !st ? '' : st.listening ? `listening for speech on ${khz(st.freq_hz)} kHz ${String(st.mode || '').toUpperCase()} · ${st.heard} spoken stretches${st.pending ? ` · ${st.pending} being transcribed` : ''}`
    : 'starting…';
}
function tDraw() {
  const showEmpty = $('tEmpty').checked;
  const rows = T.list.filter((r) => showEmpty || r.text);
  $('tlog').innerHTML = rows.slice(0, 300).map((r) => `<li class="${T.playing === r.clip ? 'playing' : ''}">
      <span class="t">${new Date(r.t0 * 1000).toLocaleTimeString([], {hour12: false})}</span>
      <span class="f">${r.freq_hz ? khz(r.freq_hz) + ' ' + String(r.mode || '').toUpperCase() : ''}</span>
      <span class="x ${r.text ? '' : 'none'}" title="${esc(r.text_hint && r.text_hint !== r.text ? 'with the radio vocabulary hint: ' + r.text_hint : '')}">${esc(r.text || '(no words)')}</span>
      <button type="button" class="smallbtn" data-day="${esc(r.day)}" data-clip="${esc(r.clip)}">${T.playing === r.clip ? 'Stop' : '▶'}</button></li>`).join('')
    || '<li><span class="x none">Nothing yet. Play a channel with Transcribe on; each spoken stretch appears here a few seconds after it ends.</span></li>';
}
async function tPoll() {
  try {
    const add = await api(`/api/ssb/transcripts?after=${T.newest}&empty=true&limit=300`);
    if (add.length) {
      T.list = [...add, ...T.list].slice(0, 600);
      T.newest = Math.max(T.newest, ...add.map((r) => r.t0));
      tDraw();
    }
  } catch (e) { /* rxapp restarting */ }
}
$('tlog').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-clip]'); if (!b) return;
  const p = $('tPlayer');
  if (T.playing === b.dataset.clip) { p.pause(); T.playing = null; return tDraw(); }
  p.src = `/api/ssb/clip/${encodeURIComponent(b.dataset.day)}/${encodeURIComponent(b.dataset.clip)}`;
  p.play().catch(() => {}); T.playing = b.dataset.clip; tDraw();
});
$('tPlayer').addEventListener('ended', () => { T.playing = null; tDraw(); });
$('tOn').addEventListener('change', async () => {
  try { await post('/api/ssb/transcribe', {on: $('tOn').checked}); } catch (e) { /* shown on the next beat */ }
  beat();
});
$('tEmpty').addEventListener('change', tDraw);
api('/api/ssb/transcribe').then((st) => { $('tOn').checked = st.enabled; tStatus(null); }).catch(() => {});
tPoll(); setInterval(tPoll, 3000); setInterval(beat, 4000);

// the spectrum tile (minispec.js): around the channel playing; nothing playing: what the receiver sees
const SSB_CONTEXT = [[1.8e6, 2e6, '160 m'], [3.5e6, 4e6, '80 m'], [5.25e6, 5.45e6, '60 m'], [7e6, 7.3e6, '40 m'], [14e6, 14.35e6, '20 m'],
  [18.068e6, 18.168e6, '17 m'], [21e6, 21.45e6, '15 m'], [24.89e6, 24.99e6, '12 m'], [28e6, 29.7e6, '10 m']];
// listening: on an amateur band, the whole amateur range (160-10 m, each band a segment, the channel's lit);
// elsewhere 300 kHz around it. The channel shaded; zoom: its passband.
function ssbContext() {
  const p = S.playing, z = miniSpec.around(p.freq, p.mode, `${p.title} · ${khz(p.freq)} kHz ${p.mode.toUpperCase()}`);
  const b = SSB_CONTEXT.find(([lo, hi]) => p.freq >= lo && p.freq <= hi);
  if (b) {
    const segs = SSB_CONTEXT.map(([lo, hi, name]) => ({lo, hi, label: name, on: lo === b[0]}));
    const lo = segs[0].lo, hi = segs[segs.length - 1].hi, pad = (hi - lo) * 0.02;
    return {lo: Math.max(0, lo - pad), hi: hi + pad, segs, label: `Amateur ${(lo / 1e6).toFixed(1)}–${(hi / 1e6).toFixed(1)} MHz · ${b[2]} · ${p.title}`,
            marks: z.marks, zoom: z};
  }
  return {lo: p.freq - 150e3, hi: p.freq + 150e3, label: `${khz(p.freq)} kHz ± 150 kHz · ${p.title}`, marks: z.marks, zoom: z};
}
// scanning: the whole of what is being scanned (Amateur: the selected bands side by side, each a segment; other
// sections: the span of their listed channels), the part being captured highlighted; zoom: just that part
const SSB_BAND_KEY = {'160m': '160 m', '80m': '80 m', '60m': '60 m', '40m': '40 m', '20m': '20 m', '17m': '17 m',
  '15m': '15 m', '12m': '12 m', '10m': '10 m'};
function scanContext() {
  const sc = S.scanning, live = miniSpec.scan();
  let segs = [];
  if (sc.sec === 'ham') {
    // each band's voice part, the range the scan searches (not the whole band: CW / data at the bottom are skipped)
    segs = sc.bands.map((b) => (S.data.ham_bands || []).find((h) => h.band === b)).filter((h) => h && h.lo_hz != null)
      .sort((a, b) => a.lo_hz - b.lo_hz).map((h) => ({lo: h.lo_hz, hi: h.hi_hz, label: SSB_BAND_KEY[h.band] || h.band,
        on: !!(live && live.lo != null && live.band_lo < h.hi_hz && live.band_hi > h.lo_hz)}));
  }
  let lo, hi;
  if (segs.length) { lo = segs[0].lo; hi = segs[segs.length - 1].hi; } else {
    const fs = S.data.channels.filter((c) => c.group === sc.sec && c.reachable).map((c) => c.freq_hz);
    if (!fs.length) return live ? miniSpec.scanRange('SSB scan') : null;
    lo = Math.min(...fs) - 20e3; hi = Math.max(...fs) + 20e3;
  }
  const pad = (hi - lo) * 0.02, name = sc.sec === 'ham' ? 'Amateur' : (S.data.groups[sc.sec] || sc.sec);
  return {lo: Math.max(0, lo - pad), hi: hi + pad, segs,
          label: `SSB scan · ${name} · ${(lo / 1e6).toFixed(2)}–${(hi / 1e6).toFixed(2)} MHz`,
          zoom: live ? miniSpec.scanRange('SSB scan, this part') : null};
}
window.miniSpecRange = () => (S.scanning ? scanContext() : S.playing ? ssbContext() : null);
