// Bay Public Safety, Live: voice_session.py's public-safety transmissions as a live transcript, a
// per-channel timeline and live audio (follow whoever is talking).
'use strict';

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

const S = {session: null, recs: [], after: -1, status: {}, live: false, sel: null, playing: null};

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
  $('log').innerHTML = items.map((r) => `<li data-i="${r.i}" class="${S.sel === r ? 'sel' : ''}">
      <span class="t">${hms(r.t0)}</span>
      <span class="tx">${r.kind === 'digital' ? '<span class="dig">digital transmission (not decoded)</span>' : r.text ? marked(r.text) : '<span class="ns">no speech recognised</span>'}</span>
      <span class="ch">${esc(chName(r.freq_hz))} · ${(r.t1 - r.t0).toFixed(1)} s</span></li>`).join('')
    || '<li><span class="t">—</span><span class="tx ns">Nothing yet. The receiver also spends time on the airband, so public-safety calls arrive in bursts.</span></li>';
}
$('log').addEventListener('click', (e) => { const li = e.target.closest('li[data-i]'); if (li) select(S.recs.find((r) => r.i === +li.dataset.i), true); });
for (const id of ['fCh', 'fDigital', 'fEmpty']) $(id).addEventListener('change', drawLog);
$('fFind').addEventListener('input', drawLog);

function select(r, pan) {
  S.sel = r;
  $('dTime').textContent = hms(r.t0);
  $('dCh').textContent = chName(r.freq_hz);
  $('dRaw').innerHTML = r.kind === 'digital' ? 'Digital transmission (P25/DMR/NXDN): logged, not decoded.'
    : r.text ? `“${marked(r.text)}”` : '(no speech recognised)';
  $('dAtc').innerHTML = r.text_atc && r.text_atc !== r.text ? `with police vocabulary hint: “${marked(r.text_atc)}”` : '';
  const codes = [...new Map([...psCodes(r.text), ...psCodes(r.text_atc)].map((c) => [c[1], c])).values()];
  $('dMeta').textContent = `${(r.t1 - r.t0).toFixed(1)} s · carrier +${r.peak_db} dB over the floor`
    + (codes.length ? ' · ' + codes.map(([, k, m]) => `${k} = ${m}`).join(', ') : '');
  const pl = (P.byI.get(r.i) || {places: []}).places;
  if (pl.length) $('dMeta').textContent += ` · places: ${pl.map((p) => placeText(p) + (p.lat == null ? (p.pending ? ' (looking up)' : ' (not found)') : '')).join('; ')}`;
  const pts = pl.filter((p) => p.lat != null).map((p) => [p.lat, p.lon]);
  if (pan && pts.length) map.setView(pts[0], Math.max(map.getZoom(), 11));
  $('clip').disabled = false;
  document.querySelectorAll('#log li').forEach((li) => li.classList.toggle('sel', +li.dataset.i === r.i));
  drawPins();
}
$('clip').addEventListener('click', () => {
  if (!S.sel) return;
  if (S.playing) S.playing.pause();
  S.playing = new Audio(`/api/safety/clip/${S.session}/${S.sel.clip}`);
  S.playing.play();
});

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
const SILENT_MS = 800;
const freqOf = (ssrc) => Object.entries(S.status.channels || {}).find(([, s]) => s === ssrc)?.[0];
function wantedSsrc() {
  const ch = S.status.channels || {};
  const mode = $('chan').value;
  if (mode !== 'follow') return ch[mode];
  const keyed = S.status.keyed || {}, cur = freqOf(audio.ssrc);
  if (cur && keyed[cur] != null) { audio.lastKey = Date.now(); return audio.ssrc; }
  if (cur && Date.now() - audio.lastKey < SILENT_MS) return audio.ssrc;
  const on = Object.entries(keyed).filter(([f]) => ch[f]).sort((a, b) => b[1] - a[1]);
  if (on.length) { audio.lastKey = Date.now(); return ch[on[0][0]]; }
  return audio.ssrc && cur ? audio.ssrc : Object.values(ch)[0];
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
$('listen').addEventListener('click', () => {
  audio.on = !audio.on;
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
$('chan').addEventListener('change', () => { if (audio.on) connect(wantedSsrc()); drawChans(); });
function followTick() {
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
  const prev = $('chan').value || 'follow';
  $('chan').innerHTML = '<option value="follow">Follow activity</option>' + freqs.map((f) => `<option value="${f}">${esc(chName(+f))}</option>`).join('');
  $('chan').value = [...$('chan').options].some((o) => o.value === prev) ? prev : 'follow';
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

// ------------------------------------------------------------------ map: places named in calls
// Towns from a built-in list (wide circles: only the town is known); highway + cross street looked up on
// OpenStreetMap by rxapp (small dots, still approximate). Colour = age; click a pin to read the call.
const map = L.map('map', {zoomSnap: 0.25}).setView([37.42, -121.98], 9.5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 16,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
for (const ms of [200, 800, 2000]) setTimeout(() => map.invalidateSize(), ms);
new ResizeObserver(() => map.invalidateSize()).observe($('map'));
const pinLayer = L.layerGroup().addTo(map);
const P = {items: [], byI: new Map()};
const ageColor = (t) => { const a = Date.now() / 1000 - t; return a < 600 ? '#D64545' : a < 3600 ? '#E0A030' : '#8A8F98'; };
const placeText = (p) => p.kind === 'city' ? p.name.replace(/\b\w/g, (c) => c.toUpperCase())
  : `${p.dir ? p.dir + 'bound ' : ''}${p.hwy} ${p.rel} ${p.cross}`.replace(/\b\w/g, (c) => c.toUpperCase());

async function pollPlaces() {
  if (!S.session) return;
  try {
    P.items = await api(`/api/safety/places?session=${S.session}`);
    P.byI = new Map(P.items.map((x) => [x.i, x]));
    drawPins();
    const n = P.items.reduce((a, x) => a + x.places.filter((p) => p.lat != null).length, 0);
    const pend = P.items.reduce((a, x) => a + x.places.filter((p) => p.pending).length, 0);
    $('mapStatus').textContent = `${n} places${pend ? ` · ${pend} being looked up` : ''}`;
  } catch (e) { $('mapStatus').textContent = 'places: ' + e.message; }
}

function drawPins() {
  pinLayer.clearLayers();
  const cut = Date.now() / 1000 - 6 * 3600;                       // the last 6 hours
  const selI = S.sel ? S.sel.i : null;
  for (const x of [...P.items].sort((a, b) => a.t0 - b.t0)) {
    if (x.t0 < cut) continue;
    for (const p of x.places) {
      if (p.lat == null) continue;
      const col = ageColor(x.t0), sel = x.i === selI;
      const tip = `<b>${hms(x.t0)}</b> · ${esc(chName(x.freq_hz))}<br><b>${esc(placeText(p))}</b><br>${x.text ? '“' + marked(x.text) + '”' : ''}`;
      const m = p.kind === 'city'
        ? L.circle([p.lat, p.lon], {radius: 2500, color: col, weight: sel ? 3 : 1.5, fillOpacity: sel ? 0.25 : 0.08, opacity: sel ? 1 : 0.7})
        : L.circleMarker([p.lat, p.lon], {radius: sel ? 9 : 6, color: sel ? '#A3316F' : '#fff', weight: 2, fillColor: col, fillOpacity: 0.95});
      m.bindTooltip(tip, {className: 'pin-tip', direction: 'top', sticky: p.kind === 'city'})
        .on('click', () => { const r = S.recs.find((y) => y.i === x.i); if (r) select(r, true); })
        .addTo(pinLayer);
    }
  }
}

// ------------------------------------------------------------------ data
async function loadSession(name) {
  const info = await api('/api/safety' + (name ? `?session=${name}` : ''));
  S.session = info.session; S.recs = []; S.after = -1; S.sel = null;
  $('session').innerHTML = info.sessions.map((s) => `<option ${s === info.session ? 'selected' : ''}>${s}</option>`).join('');
  await poll(true);
}
async function poll(full) {
  try {
    const info = await api(`/api/safety?session=${S.session}`);
    S.status = info.status || {}; S.live = info.live;
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
      const prev = $('fCh').value;
      $('fCh').innerHTML = '<option value="">all channels</option>' + freqs.map((f) => `<option value="${f}">${esc(chName(f))}</option>`).join('');
      $('fCh').value = prev;
      drawLog(); drawCodes();
    }
    followTick(); drawChans(); drawTimeline();
  } catch (e) {
    $('log').innerHTML = `<li><span class="t">—</span><span class="tx ns">No public-safety session yet (${esc(e.message)}). Start voice_session.py with a safety window.</span></li>`;
  }
}
$('session').addEventListener('change', () => loadSession($('session').value).then(pollPlaces));
loadSession().catch(() => poll(true)).finally(() => { setInterval(poll, 2000); pollPlaces(); setInterval(pollPlaces, 8000); });
