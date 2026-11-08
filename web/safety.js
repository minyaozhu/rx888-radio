// SPDX-License-Identifier: GPL-3.0-or-later
// Bay Public Safety, Live: voice_session.py's public-safety transmissions as a live transcript, a
// per-channel timeline, live audio (follow whoever is talking) and incidents on a map, each with its timeline.
'use strict';

// Refill a <select> only when its options change, and never while it is open (focused): the pages redraw
// every few hundred ms, and replacing the options under an open menu closes it and loses the choice.
function setOptions(sel, html) {
  if (sel.dataset.opts === html || document.activeElement === sel) return;
  const prev = sel.value;
  sel.innerHTML = html; sel.dataset.opts = html;
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

const $ = (id) => document.getElementById(id);
const api = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error(r.statusText); return r.json(); };
const NS = 'http://www.w3.org/2000/svg';
const el = (tag, attrs = {}, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
};
const hms = (t) => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
const mhz = (f) => (f / 1e6).toFixed(4);
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const chName = (f) => `${mhz(f)}${psLabel(f) ? ' ' + psLabel(f) : ''}`;

const S = {session: null, recs: [], after: -1, status: {}, live: false, sel: null};
const clips = clipPlayer({
  url: (r) => `/api/safety/clip/${S.session}/${r.clip}`,
  next: (r) => S.recs.find((x) => x.t0 > r.t0 && visible(x)) || null,      // play on: the next one in time
  onStart: () => listenOff(),
  list: '#log, #incTl',
  onState: (r) => { $('clip').textContent = r && S.sel && r.i === S.sel.i ? 'Stop' : 'Play clip'; },
});

// text with radio codes marked and explained on hover
function marked(text) {
  let h = esc(text);
  for (const [raw, k, meaning] of psCodes(text)) {
    h = h.replace(new RegExp(`\\b${raw.replace(/[-\s]/g, '[- ]?')}\\b`, 'i'), (m) => `<abbr class="code" title="${esc(k + ': ' + meaning)}">${m}</abbr>`);
  }
  return h;
}

// ------------------------------------------------------------------ transcript log
function visible(r) {
  if (r.kind === 'digital' && !$('fDigital').checked) return false;
  if (r.kind !== 'digital' && !r.text && !$('fEmpty').checked) return false;
  if ($('fCh').value && String(Math.round(r.freq_hz)) !== $('fCh').value) return false;
  const q = $('fFind').value.trim().toLowerCase();
  return !q || (r.text || '').toLowerCase().includes(q) || (r.text_atc || '').toLowerCase().includes(q);
}
function drawLog() {
  const items = S.recs.filter(visible).slice(-400).reverse();
  $('log').innerHTML = items.map((r) => `<li data-i="${r.i}" class="${S.sel === r ? 'sel' : ''}">${clips.button(r)}
      <span class="t">${hms(r.t0)}</span>
      <span class="tx">${r.kind === 'digital' ? '<span class="dig">digital transmission (not decoded)</span>' : r.text ? marked(r.text) : '<span class="ns">no speech recognised</span>'}</span>
      <span class="ch">${esc(chName(r.freq_hz))} · ${(r.t1 - r.t0).toFixed(1)} s</span></li>`).join('')
    || '<li><span class="t">—</span><span class="tx ns">Nothing yet. The receiver also spends time on the airband, so public-safety calls arrive in bursts.</span></li>';
}
$('log').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-i]');
  if (!li) return;
  const r = S.recs.find((x) => x.i === +li.dataset.i);
  if (e.target.closest('.clipplay')) { clips.toggle(r); if (S.sel !== r) select(r, false); return; }
  select(r, true);
});
$('playOn').addEventListener('change', (e) => { clips.playOn = e.target.checked; });
for (const id of ['fCh', 'fDigital', 'fEmpty']) $(id).addEventListener('change', () => { $(id).blur(); drawLog(); });
$('fFind').addEventListener('input', drawLog);

function select(r, pan) {
  S.sel = r;
  $('dTime').textContent = hms(r.t0);
  const act = psActivity(r.text, r.text_atc);
  $('dCh').textContent = (act ? act + ' · ' : '') + chName(r.freq_hz);
  $('dRaw').innerHTML = r.kind === 'digital' ? 'Digital transmission (P25/DMR/NXDN): logged, not decoded.'
    : r.text ? `“${marked(r.text)}”` : '(no speech recognised)';
  $('dAtc').innerHTML = r.text_atc && r.text_atc !== r.text ? `with police vocabulary hint: “${marked(r.text_atc)}”` : '';
  const codes = [...new Map([...psCodes(r.text), ...psCodes(r.text_atc)].map((c) => [c[1], c])).values()];
  $('dMeta').textContent = `${(r.t1 - r.t0).toFixed(1)} s · carrier +${r.peak_db} dB over the floor`
    + (codes.length ? ' · ' + codes.map(([, k, m]) => `${k} = ${m}`).join(', ') : '');
  const inc = I.byI.get(r.i);
  if (inc) {
    $('dMeta').textContent += ` · incident: ${inc.label}${inc.place ? ' at ' + inc.place.label : ''}`;
    if (I.sel !== inc.id) selectIncident(inc.id, pan);
  }
  $('clip').disabled = false;
  $('clip').textContent = clips.playing && clips.playing.i === r.i ? 'Stop' : 'Play clip';
  document.querySelectorAll('#log li, #incTl li').forEach((li) => li.classList.toggle('sel', +li.dataset.i === r.i));
}
$('clip').addEventListener('click', () => { if (S.sel) clips.toggle(S.sel); });

function drawCodes() {
  const seen = new Map();
  for (const r of S.recs) for (const [, k, m] of [...psCodes(r.text), ...psCodes(r.text_atc)]) seen.set(k, [m, (seen.get(k)?.[1] || 0) + 1]);
  $('codes').innerHTML = [...seen.entries()].sort((a, b) => b[1][1] - a[1][1]).slice(0, 14)
    .map(([k, [m, n]]) => `<li><b>${esc(k)}</b>${esc(m)} <span style="color:var(--muted)">×${n}</span></li>`).join('')
    || '<li style="color:var(--muted)">none yet</li>';
}

// ------------------------------------------------------------------ timeline (lanes = channels)
const tl = $('timeline');
function drawTimeline() {
  const W = 1000, LEFT = 210, RIGHT = 20, LANE = 30, TOP = 26;
  const freqs = [...new Set(S.recs.map((r) => Math.round(r.freq_hz)))].sort((a, b) => a - b);
  const t1 = Date.now() / 1000, t0 = S.recs.length ? Math.max(Math.min(S.recs[0].t0 - 30, t1 - 600), t1 - 3 * 3600) : t1 - 600;
  const H = TOP + Math.max(1, freqs.length) * LANE + 10;
  tl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  tl.innerHTML = '';
  const x = (s) => LEFT + (s - t0) / (t1 - t0) * (W - LEFT - RIGHT);
  const stepMin = [1, 2, 5, 10, 15, 30, 60].find((m) => (t1 - t0) / 60 / m <= 14) || 60;
  for (let s = Math.ceil(t0 / 60 / stepMin) * stepMin * 60; s <= t1; s += stepMin * 60) {
    el('line', {x1: x(s), x2: x(s), y1: TOP - 6, y2: H - 6, stroke: 'var(--line)', 'stroke-width': 1}, tl);
    el('text', {x: x(s), y: 14, class: 'tick', 'text-anchor': 'middle'}, tl).textContent = hms(s).slice(0, 5);
  }
  freqs.forEach((f, li) => {
    const y = TOP + li * LANE + LANE / 2;
    el('line', {x1: LEFT, x2: W - RIGHT, y1: y, y2: y, stroke: 'var(--line)', 'stroke-width': 1, 'stroke-dasharray': '2 3'}, tl);
    el('text', {x: 12, y: y - 2, class: 'lane'}, tl).textContent = mhz(f);
    el('text', {x: 12, y: y + 12, class: 'lanesub'}, tl).textContent = (psLabel(f) || '').slice(0, 30);
  });
  for (const r of S.recs) {
    if (r.t1 < t0) continue;
    const li = freqs.indexOf(Math.round(r.freq_hz));
    const c = r.kind === 'digital' ? '--c-alt' : r.text ? '--c-app' : '--c-other';
    const g = el('g', {class: 'ev' + (S.sel === r ? ' sel' : '')}, tl);
    el('rect', {x: x(r.t0), y: TOP + li * LANE + 7, width: Math.max(3, x(r.t1) - x(r.t0)), height: LANE - 14, rx: 2, fill: `var(${c})`}, g);
    el('title', {}, g).textContent = `${hms(r.t0)} ${mhz(r.freq_hz)}  ${r.kind === 'digital' ? '(digital)' : r.text || '(no speech)'}`;
    g.addEventListener('click', () => select(r));
  }
}

// ------------------------------------------------------------------ listen (follow activity)
const audio = {ctx: null, gain: null, ws: null, next: 0, ssrc: null, on: false, lastKey: 0};
const HANG_MS = 4000;
// Follow activity, tuned for FM dispatch: stay on the channel being heard through the pauses of a
// conversation (HANG_MS after it goes quiet, so a unit's reply comes through on it), and only move to a
// channel that has been on the air for MIN_KEY_MS without a break, so data bursts and kerchunks on the
// low band don't pull the audio around.
const MIN_KEY_MS = 800;
const keyedSince = {};                      // frequency -> when it was first seen keyed in this run of key-ups
function trackKeys() {
  const keyed = S.status.keyed || {}, now = Date.now();
  for (const f of Object.keys(keyedSince)) if (keyed[f] == null) delete keyedSince[f];
  for (const f of Object.keys(keyed)) if (!(f in keyedSince)) keyedSince[f] = now;
}
const keyedLongEnough = (f) => f in keyedSince && Date.now() - keyedSince[f] >= MIN_KEY_MS;

const freqOf = (ssrc) => Object.entries(S.status.channels || {}).find(([, s]) => s === ssrc)?.[0];
// How often a channel carries speech, from its recent transcripts on this page: the low band has strong
// channels that key up all day with data, tones or carrier and no words (41.1 MHz here, ~80 % without
// speech), and following the strongest keyed signal kept landing on them instead of dispatch.
const SPEECH_MIN = 0.34;                    // share of recent transmissions with words, to be worth following
const SAME_HZ = 5e3;                        // a key-up this close to a channel is that channel (grid neighbour)
const NEAR_HZ = 12.5e3;                     // its speech record includes neighbours this close: a wide data
                                            // signal shows up on several grid points, all of them wordless
const CUT_IN = [0.5, 0.7];                  // a channel usually with words (>= 0.7) cuts in on one that rarely has them (< 0.5)
function speechShare(f) {
  const hz = +f; let n = 0, words = 0;
  for (let i = S.recs.length - 1; i >= 0 && n < 40; i--) {
    const r = S.recs[i];
    if (Math.abs(r.freq_hz - hz) > NEAR_HZ) continue;
    n++;
    const t = (r.text || '').trim();
    if (r.kind !== 'digital' && t && !t.startsWith('(')) words++;
  }
  return (words + 1) / (n + 3);             // a channel not heard yet starts below SPEECH_MIN: it earns its place
}
const keyedNear = (f, keyed) => Object.keys(keyed).some((k) => Math.abs(+k - +f) <= SAME_HZ);
function wantedSsrc() {
  const ch = S.status.channels || {};
  const mode = $('chan').value;
  if (mode !== 'follow') return ch[mode];
  const keyed = S.status.keyed || {}, cur = freqOf(audio.ssrc);
  // someone else, on the air long enough and on a channel that usually carries words: the most talkative first
  const on = Object.keys(keyed).filter((f) => ch[f] && keyedLongEnough(f) && speechShare(f) >= SPEECH_MIN
    && !(cur && Math.abs(+f - +cur) <= SAME_HZ))
    .sort((a, b) => speechShare(b) - speechShare(a) || keyed[b] - keyed[a]);
  const curShare = cur ? speechShare(cur) : 0;
  if (cur && on.length && curShare < CUT_IN[0] && speechShare(on[0]) >= CUT_IN[1]) {         // dispatch beats chatter
    audio.lastKey = Date.now(); return ch[on[0]];
  }
  if (cur && keyedNear(cur, keyed)) { audio.lastKey = Date.now(); return audio.ssrc; }       // still talking
  if (cur && Date.now() - audio.lastKey < HANG_MS) return audio.ssrc;                         // a pause in the exchange
  if (on.length) { audio.lastKey = Date.now(); return ch[on[0]]; }
  if (audio.ssrc && cur) return audio.ssrc;                                                   // nothing worth hearing: stay
  const best = Object.keys(ch).sort((a, b) => speechShare(b) - speechShare(a))[0];          // starting: the most talkative
  return best ? ch[best] : null;
}
function connect(ssrc) {
  if (audio.ws) { const w = audio.ws; audio.ws = null; w.onmessage = w.onclose = null; w.close(); }
  audio.ssrc = ssrc;
  if (!ssrc || !audio.on) return;
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + `/ws/audio/${ssrc}`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    const rate = new DataView(e.data).getUint32(0, true);
    const pcm = new Int16Array(e.data, 4);
    if (!pcm.length) return;
    const buf = audio.ctx.createBuffer(1, pcm.length, rate), out = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 32768;
    const src = audio.ctx.createBufferSource();
    src.buffer = buf; src.connect(audio.gain);
    const now = audio.ctx.currentTime;
    if (audio.next < now + 0.05 || audio.next > now + 1.0) audio.next = now + 0.2;
    src.start(audio.next); audio.next += buf.duration;
  };
  ws.onclose = () => { if (audio.ws === ws && audio.on) setTimeout(() => audio.on && connect(wantedSsrc()), 1000); };
  audio.ws = ws;
}
function listenOff() {                    // another page started playing
  if (!audio.on) return;
  audio.on = false; connect(null);
  $('listen').textContent = 'Listen'; $('listen').classList.remove('on');
}
$('listen').addEventListener('click', () => {
  audio.on = !audio.on;
  if (audio.on) { clips.stop(); audioFocus.claim(listenOff); } else audioFocus.release();
  if (audio.on) {
    if (!audio.ctx) { audio.ctx = new AudioContext(); audio.gain = audio.ctx.createGain(); audio.gain.connect(audio.ctx.destination); }
    audio.ctx.resume(); audio.gain.gain.value = +$('vol').value;
    connect(wantedSsrc());
  } else connect(null);
  $('listen').textContent = audio.on ? 'Stop' : 'Listen';
  $('listen').classList.toggle('on', audio.on);
  drawChans();
});
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });
$('chan').addEventListener('change', () => { $('chan').blur(); if (audio.on) connect(wantedSsrc()); drawChans(); });
function followTick() {
  trackKeys();
  if (!audio.on || $('chan').value !== 'follow') return;
  const want = wantedSsrc();
  if (want && want !== audio.ssrc) { connect(want); drawChans(); }
}
setInterval(async () => {
  if (!audio.on || $('chan').value !== 'follow' || !S.session) return;
  try { S.status = (await api(`/api/safety?session=${S.session}`)).status || {}; followTick(); drawChans(); } catch (e) { /* main poll reports */ }
}, 300);

function drawChans() {
  const ch = S.status.channels || {}, keyed = S.status.keyed || {};
  const counts = {};
  for (const r of S.recs) counts[Math.round(r.freq_hz)] = (counts[Math.round(r.freq_hz)] || 0) + 1;
  const freqs = Object.keys(ch).sort();
  setOptions($('chan'), '<option value="follow">Follow activity</option>' + freqs.map((f) => `<option value="${f}">${esc(chName(+f))}</option>`).join(''));
  if (!$('chan').value) $('chan').value = 'follow';
  const cur = freqOf(audio.ssrc);
  $('now').textContent = audio.on && cur ? `playing ${mhz(+cur)}${keyed[cur] != null ? ' · on air' : ''}` : '';
  $('chans').innerHTML = freqs.map((f) => `<li data-f="${f}" class="${keyed[f] != null ? 'on' : ''} ${ch[f] === audio.ssrc && audio.on ? 'sel' : ''}">
      <b>${mhz(+f)}</b>${psLabel(+f) ? ' ' + esc(psLabel(+f)) : ''} · ${counts[f] || 0}${keyed[f] != null ? ' · ON AIR' : ''}</li>`).join('')
    || `<li>${S.status.listening === false ? 'not listening to public safety right now' : 'no channels yet'}</li>`;
}
$('chans').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-f]'); if (!li) return;
  $('chan').value = li.dataset.f;
  if (!audio.on) $('listen').click(); else connect(wantedSsrc());
  drawChans();
});

// ------------------------------------------------------------------ incidents (rxapp/incidents.py)
// Transmissions grouped into incidents: a type from the radio codes and words, a place (the highway junction the
// call names, from OpenStreetMap, or a town), a track when it moves, the units on it, and a timeline. Open
// incidents are bright and labelled; closed ones (cleared, cancelled, or quiet for 90 minutes) are faded.
const map = L.map('map', {zoomSnap: 0.25}).setView([37.42, -121.98], 9.5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 17,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
for (const ms of [200, 800, 2000]) setTimeout(() => map.invalidateSize(), ms);
new ResizeObserver(() => map.invalidateSize()).observe($('map'));
const incLayer = L.layerGroup().addTo(map);

const CAT = {
  help: ['#B3261E', 'Officer needs help'], collision: ['#D64545', 'Collision'], fire: ['#E8701A', 'Fire'],
  pursuit: ['#7A1F5C', 'Pursuit'], crime: ['#2E5FA8', 'Crime'], dui: ['#8E44AD', 'DUI / reckless'],
  medical: ['#2E8B57', 'Medical'], pedestrian: ['#C2185B', 'Pedestrian'], hazard: ['#B8941F', 'Road hazard'],
  disabled: ['#6F7782', 'Disabled vehicle'], stop: ['#4F7CAC', 'Traffic stop'], other: ['#8A8F98', 'Other call'],
};
const STATUS = {enroute: 'en route', onscene: 'on scene', tow: 'tow', ambulance: 'ambulance', clear: 'clear',
                cancel: 'cancelled', fire_dept: 'fire notified'};
const CLOSED = {cleared: 'cleared', cancelled: 'cancelled', quiet: 'quiet 90 min'};
const I = {list: [], byId: new Map(), byI: new Map(), sel: null, junctions: true};
const hm = (t) => hms(t).slice(0, 5);
const catColor = (c) => (CAT[c] || CAT.other)[0];

async function pollIncidents() {
  if (!S.session) return;
  try {
    const res = await api(`/api/safety/incidents?session=${S.session}`);
    if (res.session !== S.session) return;
    I.list = res.incidents; I.junctions = res.junctions;
    I.byId = new Map(I.list.map((x) => [x.id, x]));
    I.byI = new Map(I.list.flatMap((x) => x.entries.map((e) => [e.i, x])));
    drawIncidents();
    if (I.sel) drawIncident();
  } catch (e) { $('mapStatus').textContent = 'incidents: ' + e.message; }
}

function shown() {
  const h = $('incWin').value;
  const ref = S.live ? Date.now() / 1000 : Math.max(0, ...I.list.map((x) => x.t1));
  return I.list.filter((x) => h === 'all' || x.t1 >= ref - +h * 3600 || x.id === I.sel);
}

function drawIncidents() {
  const xs = shown();
  incLayer.clearLayers();
  for (const x of [...xs].sort((a, b) => (a.state === 'open') - (b.state === 'open') || a.t1 - b.t1)) {
    if (!x.place) continue;
    const col = catColor(x.cat), open = x.state === 'open', sel = x.id === I.sel;
    const ll = [x.place.lat, x.place.lon];
    L.circle(ll, {radius: x.place.radius_km * 1000, color: col, weight: sel ? 2 : 1, opacity: open ? 0.8 : 0.4,
                  fillOpacity: open ? 0.12 : 0.04, dashArray: open ? null : '4 4', interactive: false}).addTo(incLayer);
    if (x.track.length > 1) {
      L.polyline(x.track.map((p) => [p.lat, p.lon]), {color: col, weight: sel ? 3 : 2, opacity: open ? 0.8 : 0.4, dashArray: '2 6'}).addTo(incLayer);
      for (const p of x.track.slice(0, -1)) L.circleMarker([p.lat, p.lon], {radius: 3, color: col, weight: 1, fillColor: col, fillOpacity: 0.6, interactive: false}).addTo(incLayer);
    }
    const m = L.circleMarker(ll, {radius: sel ? 10 : 7, color: sel ? 'var(--ink)' : '#fff', weight: sel ? 3 : 2,
                                  fillColor: col, fillOpacity: open ? 0.95 : 0.45, opacity: open || sel ? 1 : 0.6});
    m.bindTooltip(`<b>${esc(x.label)}</b><br>${esc(x.place.label)}<br>${hm(x.t0)}–${hm(x.t1)} · ${x.n} transmission${x.n > 1 ? 's' : ''} · ${open ? 'open' : 'closed (' + (CLOSED[x.closed_by] || x.closed_by) + ')'}`,
                  {className: 'pin-tip', direction: 'top'})
      .on('click', () => selectIncident(x.id, false)).addTo(incLayer);
    if (open || sel) {
      L.tooltip({permanent: true, direction: 'right', offset: [9, 0], className: 'pin-lab' + (open ? '' : ' old')})
        .setLatLng(ll).setContent(`<b style="color:${col}">${hm(x.t0)}</b> ${esc(x.label)}<br>${esc(x.place.label)}`).addTo(incLayer);
    }
  }
  const nOpen = xs.filter((x) => x.state === 'open').length, nMap = xs.filter((x) => x.place).length;
  $('mapStatus').textContent = I.junctions ? `${xs.length} incidents · ${nOpen} open · ${nMap} on the map`
    : 'no junction table: run python -m rxapp.junctions --pbf <extract>';
  const cats = [...new Set(I.list.map((x) => x.cat))];
  $('incLegend').innerHTML = Object.entries(CAT).filter(([k]) => cats.includes(k))
    .map(([, [c, n]]) => `<span><i style="background:${c}"></i>${esc(n)}</span>`).join('');
  const rows = [...xs].sort((a, b) => b.t1 - a.t1).slice(0, 80);
  setHTML($('incs'), rows.map((x) => `<li data-id="${esc(x.id)}" class="${x.id === I.sel ? 'sel' : ''} ${x.state}">
      <span class="t">${hm(x.t0)}</span>
      <span class="a"><i style="background:${catColor(x.cat)}"></i>${esc(x.label)}</span>
      <span class="s">${x.state === 'open' ? 'open' : esc(CLOSED[x.closed_by] || 'closed')}</span>
      <span class="w">${esc(x.place ? x.place.label : 'no location heard')} · ${x.n} tx${x.units.length ? ' · ' + x.units.length + ' unit' + (x.units.length > 1 ? 's' : '') : ''}</span></li>`).join('')
    || '<li><span class="t">—</span><span class="w">No incidents recognised in this window yet.</span></li>');
  $('incCount').textContent = xs.length ? `· ${xs.length}` : '';
}
function setHTML(node, html) { if (node.dataset.html !== html) { node.innerHTML = html; node.dataset.html = html; } }

function selectIncident(id, pan) {
  I.sel = id;
  drawIncidents(); drawIncident();
  const x = I.byId.get(id);
  if (pan && x && x.place) {
    const pts = x.track.length > 1 ? x.track.map((p) => [p.lat, p.lon]) : [[x.place.lat, x.place.lon]];
    if (pts.length > 1) map.fitBounds(pts, {padding: [40, 40], maxZoom: 14});
    else map.setView(pts[0], Math.max(map.getZoom(), 12));
  }
}

function drawIncident() {
  const x = I.byId.get(I.sel);
  $('incBox').hidden = !x;
  if (!x) return;
  const col = catColor(x.cat);
  $('incHead').innerHTML = `<span class="dtime">${hm(x.t0)}–${hm(x.t1)}</span>
    <span class="dflight" style="color:${col}">${esc(x.label)}</span>
    <span class="chip" style="color:${x.state === 'open' ? col : 'var(--muted)'}">${x.state === 'open' ? 'open' : 'closed · ' + esc(CLOSED[x.closed_by] || '')}</span>`;
  const other = x.kinds.filter((k) => k !== x.label);
  $('incMeta').textContent = [x.place ? x.place.label + (x.place.kind === 'city' ? ' (town only)' : ` (± ${x.place.radius_km} km)`) : 'no location heard',
    x.units.length ? 'units ' + x.units.join(', ') : '', x.freqs.map((f) => chName(f)).join(', '),
    other.length ? 'also: ' + other.join(', ') : ''].filter(Boolean).join(' · ');
  const steps = x.status.filter((s) => STATUS[s]).map((s) => STATUS[s]);
  $('incSteps').innerHTML = ['heard', ...steps].map((s) => `<span>${esc(s)}</span>`).join('<i>›</i>');
  setHTML($('incTl'), x.entries.map((e) => {
    const r = S.recs.find((y) => y.i === e.i);
    return `<li data-i="${e.i}" class="${S.sel && S.sel.i === e.i ? 'sel' : ''}">${r ? clips.button(r) : '<span></span>'}
      <span class="t">${hms(e.t0)}</span>
      <span class="tx">${e.unit ? `<b>${esc(e.unit)}</b> ` : ''}${esc(e.text)}</span>
      <span class="ch">${[...e.status.filter((s) => STATUS[s]).map((s) => `<em>${STATUS[s]}</em>`), ...e.kinds.map(esc),
        e.place ? '📍 ' + esc(e.place) : ''].filter(Boolean).join(' · ')}</span></li>`;
  }).join(''));
}
$('incs').addEventListener('click', (e) => { const li = e.target.closest('li[data-id]'); if (li) selectIncident(li.dataset.id, true); });
$('incTl').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-i]');
  const r = li && S.recs.find((x) => x.i === +li.dataset.i);
  if (!r) return;
  if (e.target.closest('.clipplay')) { clips.toggle(r); return; }
  select(r, false);
});
$('incClose').addEventListener('click', () => { I.sel = null; drawIncidents(); drawIncident(); });
$('incWin').addEventListener('change', () => { $('incWin').blur(); drawIncidents(); });

// ------------------------------------------------------------------ data
async function loadSession(name) {
  const info = await api('/api/safety' + (name ? `?session=${name}` : ''));
  S.session = info.session; S.recs = []; S.after = -1; S.sel = null;
  $('session').innerHTML = info.sessions.map((s) => `<option ${s === info.session ? 'selected' : ''}>${s}</option>`).join('');
  await poll(true);
}
// ------------------------------------------------------------------ band switch: low band / VHF-high / both
// Holds the voice session on one public-safety window ("window:<name>"), or on both alternating ("safety").
// The RX-888 hears one input at a time, so "both" means 5 minutes each, in turn.
const fmtR = (lo, hi) => `${(lo / 1e6).toFixed(1)}–${(hi / 1e6).toFixed(1)} MHz`;
let winFocus = null;
async function drawWinSwitch() {
  const wins = (S.status.windows || []).filter((w) => w.service === 'safety');
  if (!wins.length) { $('winSeg').innerHTML = ''; return; }
  try { winFocus = (await api('/api/voice/focus')).focus; } catch (e) { /* rxapp restarting */ }
  const opts = [...wins.map((w) => ({focus: `window:${w.name}`, label: w.band === 'hf' ? 'Low band' : 'VHF-high',
                                     sub: fmtR(w.lo, w.hi), also: w.band === 'hf' ? 'hf' : null})),
                {focus: 'safety', label: 'Both', sub: 'alternate, 5 min each'}];
  const html = opts.map((o) => `<button type="button" data-focus="${o.focus}" class="${winFocus === o.focus || (o.also && winFocus === o.also) ? 'on' : ''}"
      aria-pressed="${winFocus === o.focus}">${o.label}<small>${o.sub}</small></button>`).join('');
  if ($('winSeg').dataset.html !== html) { $('winSeg').innerHTML = html; $('winSeg').dataset.html = html; }
  const held = opts.some((o) => winFocus === o.focus || (o.also && winFocus === o.also));
  $('winNote').textContent = held ? '' : winFocus ? 'the receiver is held elsewhere; pick a band to bring it here' : 'the receiver is rotating through every page';
}
$('winSeg').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-focus]'); if (!b) return;
  for (const x of $('winSeg').querySelectorAll('button')) x.disabled = true;
  try {
    const r = await fetch('/api/voice/focus', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({focus: b.dataset.focus})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  } catch (err) { $('winNote').textContent = 'could not switch: ' + err.message; }
  $('winSeg').dataset.html = ''; await drawWinSwitch();
});

async function poll(full) {
  try {
    const info = await api(`/api/safety?session=${S.session}`);
    S.status = info.status || {}; S.live = info.live;
    drawWinSwitch();
    $('fLive').className = 'live' + (S.live ? '' : ' off');
    $('fLive').lastChild.textContent = S.live ? 'live' : 'recorded';
    const st = S.status, left = st.next_switch ? Math.max(0, st.next_switch - Date.now() / 1000) : null;
    const clock = left != null ? `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, '0')}` : '';
    $('fWin').innerHTML = !S.live ? '–' : st.listening
      ? `${esc(st.window)} ${mhz(st.lo)}–${mhz(st.hi)} MHz${clock ? ' · next switch in ' + clock : ''}`
      : `<span class="away">away on ${esc(st.window)} (${esc(st.window_service)})${clock ? ' · next switch in ' + clock : ''}</span>`;
    const add = await api(`/api/safety/transmissions?session=${S.session}&after=${S.after}`);
    if (add.length || full) {
      S.recs.push(...add);
      S.after = S.recs.length ? S.recs[S.recs.length - 1].i : -1;
      $('nTx').textContent = S.recs.length;
      const freqs = [...new Set(S.recs.map((r) => Math.round(r.freq_hz)))].sort((a, b) => a - b);
      $('nCh').textContent = freqs.length;
      setOptions($('fCh'), '<option value="">all channels</option>' + freqs.map((f) => `<option value="${f}">${esc(chName(f))}</option>`).join(''));
      drawLog(); drawCodes();
    }
    followTick(); drawChans(); drawTimeline();
  } catch (e) {
    $('log').innerHTML = `<li><span class="t">—</span><span class="tx ns">No public-safety session yet (${esc(e.message)}). Start voice_session.py with a safety window.</span></li>`;
  }
}
$('session').addEventListener('change', () => { I.sel = null; I.list = []; drawIncident(); loadSession($('session').value).then(pollIncidents); });
loadSession().catch(() => poll(true)).finally(() => { setInterval(poll, 2000); pollIncidents(); setInterval(pollIncidents, 8000); });
