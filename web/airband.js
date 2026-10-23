// Bay Approach Live: airband_session.py's transmissions on the Quiet Bridge map and a per-flight
// timeline, updated as they arrive, plus live audio of the loudest (or the talking) channel.
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
const mhz = (f) => (f / 1e6).toFixed(3);
const esc = (x) => String(x).replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);

// ------------------------------------------------------------------ phases (as on the original page)
const PH = {
  check: {label: 'Check-in', c: '--c-check', stage: 0},
  alt: {label: 'Speed / altitude', c: '--c-alt', stage: 1},
  vec: {label: 'Vector', c: '--c-vec', stage: 2},
  trf: {label: 'Traffic / visual', c: '--c-trf', stage: 3},
  app: {label: 'Approach / cleared', c: '--c-app', stage: 4},
  hand: {label: 'Handoff', c: '--c-hand', stage: 5},
  other: {label: 'Other / unclear', c: '--c-other', stage: null},
};
const RULES = [   // checked from the last stage back: a call that clears AND hands off is a handoff
  ['hand', /contact (the )?tower|over to (the )?tower|tower (one |1)?(two |2)?\d|\b1?20\.5\b|good day|so long|contact (norcal|oakland|bay)|to the bridge/],
  ['app', /cleared|clear(ed)? (for )?(the )?visual|visual approach|quiet bridge|\bils\b|localizer|runway (two eight|28)|\b28 ?[lr]\b|two eight (left|right)|join the/],
  ['trf', /traffic|in ?sight|airport in|field in|report (the )?(airport|field)|visual separation|follow(ing)? the/],
  ['vec', /heading|turn (left|right)|\bdirect\b|vector|fly (present )?heading/],
  ['alt', /descend|climb|maintain|\b\d{1,2},?000\b|knots|speed|slow|reduce|\b(1[5-9]0|2[0-5]0)\b/],
  ['check', /with you|checking in|norcal approach|out of \d|level \d|information [a-z]+/],
];
function phaseOf(text) {
  const t = text.toLowerCase();
  for (const [k, re] of RULES) if (re.test(t)) return k;
  return 'other';
}

// ------------------------------------------------------------------ call signs
const AIRLINES = {
  united: 'UAL', southwest: 'SWA', alaska: 'ASA', skywest: 'SKW', 'sky west': 'SKW', delta: 'DAL', american: 'AAL',
  jetblue: 'JBU', 'jet blue': 'JBU', frontier: 'FFT', spirit: 'NKS', hawaiian: 'HAL', fedex: 'FDX', 'fed ex': 'FDX',
  ups: 'UPS', horizon: 'QXE', 'air canada': 'ACA', cathay: 'CPA', eva: 'EVA', korean: 'KAL', 'all nippon': 'ANA',
  'japan air': 'JAL', lufthansa: 'DLH', speedbird: 'BAW', emirates: 'UAE', qantas: 'QFA', singapore: 'SIA',
  shamrock: 'EIN', westjet: 'WJA', 'sun country': 'SCX', allegiant: 'AAY', breeze: 'MXY', compass: 'CPZ',
  envoy: 'ENY', republic: 'RPA', cactus: 'AWE', 'air france': 'AFR', klm: 'KLM', condor: 'CFG', 'virgin': 'VIR',
  'china airlines': 'CAL', 'china southern': 'CSN', 'air china': 'CCA', 'philippine': 'PAL', 'turkish': 'THY',
};
// Whisper often splits a flight number: "United 15, 75", "American 24-10" -> 1575, 2410
const AIRLINE_RE = new RegExp(`\\b(${Object.keys(AIRLINES).sort((a, b) => b.length - a.length).join('|')})\\s*(?:flight\\s*)?(\\d{1,2}[ ,-]+\\d{2}(?!\\d)|\\d{1,4})\\b`, 'i');
const title = (s) => s.replace(/\b\w/g, (c) => c.toUpperCase());
function callsignOf(texts) {
  for (const t of texts) {
    const m = t && t.match(AIRLINE_RE);
    if (m) { const n = m[2].replace(/\D/g, ''); return {id: AIRLINES[m[1].toLowerCase()] + n, name: `${title(m[1].toLowerCase())} ${n}`, num: n}; }
  }
  // a read-back ends with the call sign: "…, 664." Not altitudes, speeds or headings.
  for (const t of texts) {
    const last = (t || '').trim().replace(/[.,!?]+$/, '').split(/[\s,]+/).pop() || '';
    if (/^\d{3,4}$/.test(last) && !/00$/.test(last) && !(+last <= 360 && +last % 10 === 0)) return {id: last, name: '…' + last, num: last};
  }
  return null;
}

// ------------------------------------------------------------------ state
const S = {session: null, recs: [], after: -1, status: {}, live: false, follow: true, cursor: null, sel: null,
           flights: [], fIndex: new Map(), playing: null};

function derive() {
  const named = new Map();     // number -> full id, so "…2004" joins "United 2004"
  for (const r of S.recs) {
    r.texts = [r.text || '', r.text_atc || ''];
    r.cs = callsignOf(r.texts);
    r.ph = phaseOf(r.texts.join(' | '));
    if (r.cs && r.cs.id !== r.cs.num) named.set(r.cs.num, r.cs);
  }
  const flights = new Map();
  for (const r of S.recs) {
    if (r.cs && r.cs.id === r.cs.num && named.has(r.cs.num)) r.cs = named.get(r.cs.num);
    r.f = r.cs ? r.cs.id : '?';
    if (!flights.has(r.f)) flights.set(r.f, {id: r.f, name: r.cs ? r.cs.name : 'Unattributed', first: r.t0, n: 0, freqs: new Set()});
    const f = flights.get(r.f);
    f.n++; f.freqs.add(r.freq_hz);
  }
  const list = [...flights.values()].filter((f) => f.id !== '?').sort((a, b) => a.first - b.first);
  if (flights.has('?')) list.push(flights.get('?'));
  S.flights = list;
  S.fIndex = new Map(list.map((f, i) => [f.id, i]));
  $('nTx').textContent = S.recs.length;
  $('nFlights').textContent = list.filter((f) => f.id !== '?').length;
}

// ------------------------------------------------------------------ timeline
const tl = $('timeline');
const LEFT = 150, RIGHT = 20, LANE = 32, TOP = 26, W = 1000;
function range() {
  const now = Date.now() / 1000;
  const t1 = S.live ? now : (S.recs.length ? S.recs[S.recs.length - 1].t1 + 30 : now);
  const t0 = S.recs.length ? Math.min(S.recs[0].t0 - 30, t1 - 300) : t1 - 300;
  return [Math.max(t0, t1 - 3 * 3600), t1];
}
function drawTimeline() {
  const [T0, T1] = range();
  const H = TOP + Math.max(1, S.flights.length) * LANE + 10;
  tl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  tl.innerHTML = '';
  const x = (s) => LEFT + (s - T0) / (T1 - T0) * (W - LEFT - RIGHT);
  const stepMin = [1, 2, 5, 10, 15, 30, 60].find((m) => (T1 - T0) / 60 / m <= 14) || 60;
  for (let s = Math.ceil(T0 / 60 / stepMin) * stepMin * 60; s <= T1; s += stepMin * 60) {
    el('line', {x1: x(s), x2: x(s), y1: TOP - 6, y2: H - 6, stroke: 'var(--line)', 'stroke-width': 1}, tl);
    el('text', {x: x(s), y: 14, class: 'tick', 'text-anchor': 'middle'}, tl).textContent = hms(s).slice(0, 5);
  }
  S.flights.forEach((f, li) => {
    const y = TOP + li * LANE + LANE / 2;
    el('line', {x1: LEFT, x2: W - RIGHT, y1: y, y2: y, stroke: 'var(--line)', 'stroke-width': 1, 'stroke-dasharray': '2 3'}, tl);
    el('text', {x: 12, y: y - 2, class: 'lane'}, tl).textContent = f.name;
    el('text', {x: 12, y: y + 12, class: 'lanesub'}, tl).textContent = `${f.n} calls · ${[...f.freqs].map(mhz).join(' ')}`;
  });
  for (const r of S.recs) {
    const li = S.fIndex.get(r.f);
    if (li == null || r.t1 < T0) continue;
    const g = el('g', {class: 'ev' + (S.sel === r ? ' sel' : ''), tabindex: 0, role: 'button', 'aria-label': `${hms(r.t0)} ${r.text || ''}`}, tl);
    el('circle', {cx: x(r.t0), cy: TOP + li * LANE + LANE / 2, r: Math.max(4, Math.min(8, 2 + (r.t1 - r.t0))),
                  fill: `var(${PH[r.ph].c})`, stroke: 'var(--panel)', 'stroke-width': 1.5, opacity: r.t0 <= S.cursor ? 1 : 0.22}, g);
    el('title', {}, g).textContent = `${hms(r.t0)}  ${mhz(r.freq_hz)}  ${r.text || '(no speech)'}`;
    g.addEventListener('click', () => select(r, true));
  }
  el('line', {x1: x(S.cursor), x2: x(S.cursor), y1: TOP - 8, y2: H - 6, stroke: 'var(--magenta)', 'stroke-width': 2}, tl);
  $('scrub').value = S.follow ? 1000 : Math.round((S.cursor - T0) / (T1 - T0) * 1000);
  $('clock').textContent = hms(S.cursor) + (S.follow && S.live ? ' · live' : '');
}

// ------------------------------------------------------------------ map
// Real Bay Area map: airports at their reference points (lit while one of their frequencies is on
// the air, with the latest words), flights at the airport whose tower/ground they're talking to, or
// along the SFO Quiet Bridge arrival flow when they're with NorCal Approach.
// zoomSnap 0.25: finer zoom steps, so a saved view can be just right
const map = L.map('map', {zoomControl: true, attributionControl: true, zoomSnap: 0.25, zoomDelta: 0.5}).setView([37.62, -122.16], 9);
// Leaflet measures its box when created; fonts and the panel layout settle later, so measure again
// Default view: whatever was saved with ★ (kept in this browser), else DEFAULT_VIEW
const VIEW_KEY = 'airband.mapView';
function savedView() {
  try { const v = JSON.parse(localStorage.getItem(VIEW_KEY)); return v && isFinite(v.lat) && isFinite(v.zoom) ? v : null; }
  catch (e) { return null; }
}
// The built-in default: the view chosen on 2026-10-23 (the bay, Foster City to Fremont, zoom 10)
const DEFAULT_VIEW = {lat: 37.563, lon: -122.156, zoom: 10};
const homeView = () => { const v = savedView() || DEFAULT_VIEW; map.setView([v.lat, v.lon], v.zoom, {animate: false}); };
homeView();
for (const ms of [200, 800, 2000]) setTimeout(() => { map.invalidateSize(); if (ms === 800) homeView(); }, ms);
const ViewControl = L.Control.extend({
  options: {position: 'topleft'},
  onAdd() {
    const box = L.DomUtil.create('div', 'leaflet-bar viewctl');
    const save = L.DomUtil.create('a', '', box), home = L.DomUtil.create('a', '', box);
    save.href = home.href = '#';
    save.textContent = '★'; save.title = 'Save this view as the default';
    home.textContent = '⌂'; home.title = 'Back to the default view (double-click: forget a saved one)';
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.on(save, 'click', (e) => {
      L.DomEvent.preventDefault(e);
      const c = map.getCenter();
      try { localStorage.setItem(VIEW_KEY, JSON.stringify({lat: +c.lat.toFixed(5), lon: +c.lng.toFixed(5), zoom: map.getZoom()})); } catch (err) { /* private mode */ }
      save.textContent = '✓'; setTimeout(() => { save.textContent = '★'; }, 1200);
    });
    L.DomEvent.on(home, 'click', (e) => { L.DomEvent.preventDefault(e); homeView(); });
    L.DomEvent.on(home, 'dblclick', (e) => {
      L.DomEvent.preventDefault(e);
      try { localStorage.removeItem(VIEW_KEY); } catch (err) { /* ignore */ }
      homeView();
    });
    return box;
  },
});
new ViewControl().addTo(map);
new ResizeObserver(() => map.invalidateSize()).observe($('map'));
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {      // darkened by CSS in dark mode
  maxZoom: 15, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
// SFO Quiet Bridge Visual 28R, roughly: up from the south-east, along the San Mateo Bridge, to SFO
const STAGE = [[37.30, -121.86], [37.42, -121.97], [37.52, -122.03], [37.61, -122.08], [37.585, -122.20], [37.60, -122.28]];
L.polyline(STAGE.concat([[37.6188, -122.3754]]), {color: cssv('--magenta'), weight: 2, dashArray: '6 6', opacity: 0.5})
  .addTo(map).bindTooltip('SFO arrivals: Quiet Bridge Visual 28R (schematic)', {sticky: true});
const STAGE_NAMES = ['check-in', 'slow · descend', 'vectors', 'base', 'join · in sight', 'at the bridge → tower'];
const APT = {};
for (const a of BAY_AIRPORTS) {
  const m = L.circleMarker([a.lat, a.lon], {radius: 7, color: cssv('--blue'), weight: 2, fillColor: cssv('--panel'), fillOpacity: 1, className: 'apt-dot'}).addTo(map);
  m.bindTooltip(a.id, {permanent: true, direction: 'right', offset: [6, 0], className: 'apt-tip'});
  m.bindPopup(`<b>${a.name} (${a.id})</b><br>` + Object.entries(a.f).map(([f, r]) => `${f} ${r}`).join('<br>'));
  APT[a.id] = {a, m, say: null};
}
const pl = {};

function planeIcon(name, sel) {
  return L.divIcon({className: 'plane-ic' + (sel ? ' sel' : ''), html: `<div><i></i>${esc(name.replace('…', ''))}</div>`, iconSize: null, iconAnchor: [7, 7]});
}

function drawMap() {
  const t = S.cursor;
  // airports: lit while a frequency of theirs is on the air (live) or transmitted in the last 8 s (replay)
  const lit = {};
  if (S.follow && S.live) for (const f of Object.keys(S.status.keyed || {})) { const i = freqInfo(+f); if (i && i.airport) lit[i.airport.id] = true; }
  const recent = S.recs.filter((r) => r.t0 <= t && t - r.t1 < 8);
  for (const r of recent) { const i = freqInfo(r.freq_hz); if (i && i.airport) lit[i.airport.id] = r; }
  for (const [id, A] of Object.entries(APT)) {
    const on = !!lit[id];
    A.m.setStyle({color: on ? cssv('--magenta') : cssv('--blue'), radius: on ? 10 : 7});
    const el = A.m.getElement(); if (el) el.classList.toggle('on', on);
    const tip = A.m.getTooltip(); if (tip && tip.getElement()) tip.getElement().classList.toggle('on', on);
    const r = typeof lit[id] === 'object' ? lit[id] : null;
    if (r && r.text) {
      const html = `<b>${esc(freqInfo(r.freq_hz).label)}</b>${esc(r.text)}`;
      if (!A.say) A.say = L.tooltip({permanent: true, direction: 'top', offset: [0, -10], className: 'say'}).setLatLng([A.a.lat, A.a.lon]).setContent(html).addTo(map);
      else A.say.setContent(html);
    } else if (A.say) { A.say.remove(); A.say = null; }
  }
  // approach/departure sectors (not tied to one airport)
  const secOn = {};
  for (const r of recent) { const i = freqInfo(r.freq_hz); if (i && i.sector) secOn[`${i.sector} ${mhz(r.freq_hz)}`] = true; }
  if (S.follow && S.live) for (const f of Object.keys(S.status.keyed || {})) { const i = freqInfo(+f); if (i && i.sector) secOn[`${i.sector} ${mhz(+f)}`] = true; }
  const heardSec = [...new Set(S.recs.map((r) => { const i = freqInfo(r.freq_hz); return i && i.sector ? `${i.sector} ${mhz(r.freq_hz)}` : null; }).filter(Boolean))].sort();
  $('sectors').innerHTML = heardSec.map((s) => `<span class="${secOn[s] ? 'on' : ''}">${s}${secOn[s] ? ' · ON AIR' : ''}</span>`).join('');

  // flights
  const occupied = {};
  const list = $('flightList');
  list.innerHTML = '';
  const seen = new Set();
  for (const f of S.flights) {
    if (f.id === '?') continue;
    const past = S.recs.filter((r) => r.f === f.id && r.t0 <= t);
    seen.add(f.id);
    const p = pl[f.id];
    if (!past.length || t - past[past.length - 1].t0 > 20 * 60) { if (p) { p.remove(); delete pl[f.id]; } continue; }
    const lastAny = past[past.length - 1];
    const fi = freqInfo(lastAny.freq_hz);
    const real = adsbFor(f.id);
    if (real) {                                                // real position from ADS-B: no guessing
      if (p) { p.remove(); delete pl[f.id]; }
      const st = acAt(real, t);
      const li = document.createElement('li');
      li.innerHTML = `<span><b>${esc(f.name)}</b> <span style="color:var(--magenta)">ADS-B</span></span><span>${st ? esc(acLine(real, st)) : 'no position'} · ${hms(lastAny.t0)}</span>`;
      li.style.cursor = 'pointer';
      li.addEventListener('click', () => select(lastAny));
      list.appendChild(li);
      continue;
    }
    let pos, where;
    if (fi && fi.airport) {                                   // talking to a tower / ground
      const key = 'apt' + fi.airport.id, n = (occupied[key] = (occupied[key] || 0) + 1) - 1;
      pos = [fi.airport.lat + 0.012 + 0.012 * Math.floor(n / 3), fi.airport.lon + 0.02 * (n % 3) - 0.02];
      where = fi.label;
    } else {                                                   // with approach: along the SFO flow
      const last = [...past].reverse().find((r) => PH[r.ph].stage !== null);
      const stage = last ? PH[last.ph].stage : 0, at = last ? last.t0 : past[0].t0;
      const gone = stage === 5 && t - at > 90;
      const key = gone ? 'tower' : stage, n = (occupied[key] = (occupied[key] || 0) + 1) - 1;
      const [la, lo] = gone ? [37.6188, -122.3754] : STAGE[stage];
      pos = [la + 0.012 * (n % 3), lo + 0.02 * Math.floor(n / 3)];
      where = gone ? 'With SFO Tower' : `${fi ? fi.label : mhz(lastAny.freq_hz)} · ${last ? PH[last.ph].label : 'heard'}`;
    }
    const sel = S.sel && S.sel.f === f.id;
    if (!p) {
      pl[f.id] = L.marker(pos, {icon: planeIcon(f.name, sel), zIndexOffset: 500}).addTo(map)
        .on('click', () => { const r = S.recs.filter((x) => x.f === f.id && x.t0 <= S.cursor).pop(); if (r) select(r); });
    } else { p.setLatLng(pos); p.setIcon(planeIcon(f.name, sel)); }
    const li = document.createElement('li');
    li.innerHTML = `<span><b>${esc(f.name)}</b></span><span>${esc(where)} · ${hms(lastAny.t0)}</span>`;
    li.style.cursor = 'pointer';
    li.addEventListener('click', () => select(lastAny));
    list.appendChild(li);
  }
  for (const id in pl) if (!seen.has(id)) { pl[id].remove(); delete pl[id]; }
  if (!list.children.length) list.innerHTML = '<li><span>No flights identified yet</span><span></span></li>';
}

// ------------------------------------------------------------------ aircraft (ADS-B, adsb.fi)
// Real positions from the adsb.fi open-data feed (rxapp polls it while this page is open). Planes point
// along their track, coloured by altitude, with their recent track as a trail. When the timeline is
// scrubbed back, planes sit where they were then. A flight named in a transcript ("American 2105") is
// matched to its ADS-B call sign (AAL2105) and highlighted; its inferred position is dropped.
const AC = {planes: new Map(), since: 0, show: true, trails: true, fetched: null, error: null};
const acLayer = L.layerGroup().addTo(map), trailLayer = L.layerGroup().addTo(map);
map.attributionControl.addAttribution('Aircraft: <a href="https://adsb.fi" target="_blank">adsb.fi</a> open data');
const ALT_BANDS = [[0, '#8A8F98', 'ground'], [1, '#2E9E5B', '< 2,000 ft'], [2000, '#9BBF2E', '2–5,000'], [5000, '#E0A030', '5–10,000'],
                   [10000, '#E0662E', '10–20,000'], [20000, '#3D7FD6', '20–30,000'], [30000, '#7B5BD6', '30,000+']];
// barometric altitude near sea level can read below zero (-200 ft): that's the lowest airborne band
const altColor = (a) => a.ground ? ALT_BANDS[0][1] : ([...ALT_BANDS].reverse().find(([lo]) => (a.alt ?? 0) >= lo) || ALT_BANDS[1])[1];
const PLANE_PATH = 'M0,-9 L1.6,-3 L9,1.2 L9,3 L1.6,1.2 L1,6.5 L3.5,8.6 L3.5,9.8 L0,9 L-3.5,9.8 L-3.5,8.6 L-1,6.5 L-1.6,1.2 L-9,3 L-9,1.2 L-1.6,-3 Z';

async function pollAdsb() {
  try {
    const d = await api(`/api/adsb?since=${AC.since}`);
    AC.fetched = d.fetched; AC.error = d.error;
    const here = new Set();
    for (const a of d.aircraft) {
      here.add(a.hex);
      const e = AC.planes.get(a.hex) || {trail: [], marker: null, line: null, key: ''};
      const lastT = e.trail.length ? e.trail[e.trail.length - 1][0] : 0;
      for (const p of a.trail) if (p[0] > lastT) e.trail.push(p);
      const cut = Date.now() / 1000 - 20 * 60;
      while (e.trail.length && e.trail[0][0] < cut) e.trail.shift();
      e.a = a;
      AC.planes.set(a.hex, e);
    }
    for (const [hx, e] of AC.planes) if (!here.has(hx)) { e.marker && e.marker.remove(); e.line && e.line.remove(); AC.planes.delete(hx); }
    AC.since = d.now - 30;          // a little overlap: a position can arrive a few seconds late
    drawAircraft();
  } catch (e) { AC.error = e.message; }
  $('acStatus').textContent = AC.error ? `ADS-B: ${AC.error}` : `${AC.planes.size} aircraft · adsb.fi`;
}

// position at time t: the latest state when live, else the last track point at or before t (within 60 s)
function acAt(e, t) {
  if (S.follow && S.live) return {lat: e.a.lat, lon: e.a.lon, alt: e.a.alt, ground: e.a.ground, track: e.a.track};
  let i = e.trail.length - 1;
  while (i >= 0 && e.trail[i][0] > t) i--;
  if (i < 0 || t - e.trail[i][0] > 60) return null;
  const [pt, lat, lon, alt] = e.trail[i], nx = e.trail[Math.max(0, i - 1)];
  const track = i > 0 ? (Math.atan2((lon - nx[2]) * Math.cos(lat * Math.PI / 180), lat - nx[1]) * 180 / Math.PI + 360) % 360 : e.a.track;
  return {lat, lon, alt, ground: alt === 0, track, t: pt};
}

function nearestAirport(lat, lon) {
  let best = null, bd = 1e9;
  for (const a of BAY_AIRPORTS) {
    const dy = (a.lat - lat) * 111, dx = (a.lon - lon) * 111 * Math.cos(lat * Math.PI / 180), d = Math.hypot(dx, dy);
    if (d < bd) { bd = d; best = {a, km: d, brg: (Math.atan2(-dx, -dy) * 180 / Math.PI + 360) % 360}; }
  }
  return best;
}
const compass = (b) => ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(b / 45) % 8];
function acLine(e, st) {
  const n = nearestAirport(st.lat, st.lon);
  const alt = st.ground ? 'on the ground' : st.alt != null ? `${Math.round(st.alt).toLocaleString()} ft` : '';
  const gs = e.a.gs != null && (S.follow && S.live) ? `, ${Math.round(e.a.gs)} kt` : '';
  return `${e.a.flight || e.a.hex} ${e.a.type} ${alt}${gs}, ${n.km < 1.5 ? 'at ' + n.a.id : n.km.toFixed(0) + ' km ' + compass(n.brg) + ' of ' + n.a.id}`;
}

// transcript flight id (UAL2105, or a bare number "2105") -> the ADS-B aircraft with that call sign
function adsbFor(fid) {
  if (!fid || fid === '?') return null;
  let hit = null, n = 0;
  for (const e of AC.planes.values()) {
    const cs = e.a.flight || '';
    if (cs === fid) return e;
    if (/^\d+$/.test(fid) && /^[A-Z]{3}\d/.test(cs) && cs.slice(3) === fid) { hit = e; n++; }
  }
  return n === 1 ? hit : null;          // a bare number counts only if exactly one airline flight has it
}

function drawAircraft() {
  if (!AC.show) { acLayer.clearLayers(); trailLayer.clearLayers(); AC.planes.forEach((e) => { e.marker = e.line = null; }); return; }
  const t = S.cursor, z = map.getZoom();
  const matched = new Map();             // hex -> transcript flight
  for (const f of S.flights) { const e = adsbFor(f.id); if (e) matched.set(e.a.hex, f); }
  const selHex = S.sel && S.sel.f ? adsbFor(S.sel.f)?.a.hex : null;
  for (const [hx, e] of AC.planes) {
    const st = acAt(e, t);
    if (!st) { if (e.marker) { e.marker.remove(); e.marker = null; } if (e.line) { e.line.remove(); e.line = null; } continue; }
    const f = matched.get(hx), sel = hx === selHex, col = altColor(st);
    const label = f ? f.name : (z >= 11 ? (e.a.flight || '') : '');
    const key = [Math.round(st.track ?? 0), col, f ? 1 : 0, sel ? 1 : 0, label].join('|');
    if (!e.marker) {
      e.marker = L.marker([st.lat, st.lon], {zIndexOffset: f ? 900 : 600}).addTo(acLayer)
        .bindTooltip('', {direction: 'top', offset: [0, -10]})
        .on('click', () => { const f2 = matched.get(hx); if (f2) { const r = S.recs.filter((x) => x.f === f2.id && x.t0 <= S.cursor).pop(); if (r) select(r); } });
    } else e.marker.setLatLng([st.lat, st.lon]);
    if (e.key !== key) {
      const ring = f ? `<circle r="12" fill="none" stroke="${cssv('--magenta')}" stroke-width="${sel ? 3 : 2}"/>` : '';
      e.marker.setIcon(L.divIcon({className: 'ac-ic', iconSize: [26, 26], iconAnchor: [13, 13],
        html: `<svg width="26" height="26" viewBox="-13 -13 26 26">${ring}<path d="${PLANE_PATH}" transform="rotate(${Math.round(st.track ?? 0)})" fill="${col}" stroke="${cssv('--panel')}" stroke-width="0.8"/></svg>`
          + (label ? `<span class="ac-lab${f ? ' m' : ''}">${esc(label)}</span>` : '')}));
      e.key = key;
    }
    e.marker.setTooltipContent(`${esc(acLine(e, st))}${e.a.reg ? ' · ' + esc(e.a.reg) : ''}${e.a.squawk ? ' · squawk ' + e.a.squawk : ''}${f ? '<br>heard as <b>' + esc(f.name) + '</b>' : ''}`);
    // trail: the track up to time t
    if (AC.trails) {
      const pts = e.trail.filter((p) => p[0] <= t + 1).map((p) => [p[1], p[2]]);
      if (pts.length >= 2) {
        const style = {color: f ? cssv('--magenta') : col, weight: f || sel ? 3 : 1.5, opacity: f ? 0.85 : 0.45};
        if (!e.line) e.line = L.polyline(pts, {...style, interactive: false}).addTo(trailLayer);
        else { e.line.setLatLngs(pts); e.line.setStyle(style); }
      } else if (e.line) { e.line.remove(); e.line = null; }
    } else if (e.line) { e.line.remove(); e.line = null; }
  }
}
$('acShow').addEventListener('change', () => { AC.show = $('acShow').checked; drawAircraft(); });
$('acTrails').addEventListener('change', () => { AC.trails = $('acTrails').checked; drawAircraft(); });
map.on('zoomend', () => { AC.planes.forEach((e) => { e.key = ''; }); drawAircraft(); });
$('acLegend').innerHTML = ALT_BANDS.map(([, c, n]) => `<span><i style="background:${c}"></i>${n}</span>`).join('');
pollAdsb(); setInterval(pollAdsb, 5000);

// ------------------------------------------------------------------ detail, log
function select(r, moveTime) {
  S.sel = r;
  if (moveTime && !(S.follow && S.live)) S.cursor = r.t0 + 0.5;
  $('dTime').textContent = hms(r.t0);
  $('dFlight').textContent = r.cs ? r.cs.name : 'Unattributed';
  $('dPhase').textContent = PH[r.ph].label; $('dPhase').style.color = `var(${PH[r.ph].c})`;
  $('dRaw').textContent = r.text ? `“${r.text}”` : '(no speech recognised)';
  $('dAtc').textContent = r.text_atc && r.text_atc !== r.text ? `with ATC hint: “${r.text_atc}”` : '';
  $('dMeta').textContent = `${mhz(r.freq_hz)} MHz${freqInfo(r.freq_hz) ? ' (' + freqInfo(r.freq_hz).label + ')' : ''} · ${(r.t1 - r.t0).toFixed(1)} s · carrier +${r.peak_db} dB over the floor`;
  const real = r.f ? adsbFor(r.f) : null, st = real ? acAt(real, r.t0) : null;
  if (st) $('dMeta').textContent += `\nADS-B (adsb.fi): ${acLine(real, st)}`;
  $('dMeta').style.whiteSpace = 'pre-line';
  $('clip').disabled = false;
  document.querySelectorAll('#log li').forEach((li) => li.classList.toggle('sel', +li.dataset.i === r.i));
  render();
}
function drawLog() {
  const items = S.recs.slice(-300).reverse();
  $('log').innerHTML = items.map((r) => `<li data-i="${r.i}" class="${S.sel === r ? 'sel' : ''}"><span class="t">${hms(r.t0)}</span>
    <span class="f">${mhz(r.freq_hz)} ${esc(freqInfo(r.freq_hz)?.label.split(' ').slice(0, 2).join(' ') || '')}</span><span>${r.text ? esc(r.text) : '<span class="t">(no speech)</span>'}</span></li>`).join('')
    || '<li><span class="t">—</span><span></span><span class="t">Nothing yet.</span></li>';
}
$('log').addEventListener('click', (e) => { const li = e.target.closest('li[data-i]'); if (li) select(S.recs.find((r) => r.i === +li.dataset.i), true); });
$('clip').addEventListener('click', () => {
  if (!S.sel) return;
  if (S.playing) S.playing.pause();
  S.playing = new Audio(`/api/airband/clip/${S.session}/${S.sel.clip}`);
  S.playing.play();
});

function render() {
  if (S.follow && S.live) S.cursor = Date.now() / 1000;
  drawTimeline(); drawMap(); drawAircraft();
}

// ------------------------------------------------------------------ channels + live audio
const audio = {ctx: null, gain: null, ws: null, next: 0, ssrc: null, on: false, lastKey: 0};
function loudest() {
  const t = Date.now() / 1000 - 30 * 60;
  const by = {};
  for (const r of S.recs) if (r.t0 > t || !S.live) (by[r.freq_hz] = by[r.freq_hz] || []).push(r.peak_db);
  let best = null, bv = -1;
  for (const [f, v] of Object.entries(by)) {
    const med = v.sort((a, b) => a - b)[Math.floor(v.length / 2)] + Math.log2(v.length);   // strong and busy
    if (med > bv && S.status.channels && S.status.channels[Math.round(+f)]) { bv = med; best = Math.round(+f); }
  }
  return best;
}
// Follow: stay on the channel being heard while it's active; once it has been quiet for SILENT_MS,
// jump to whichever channel is on the air (the strongest, if several). Nobody on the air: stay put.
const SILENT_MS = 800;
const freqOf = (ssrc) => Object.entries(S.status.channels || {}).find(([, s]) => s === ssrc)?.[0];
function wantedSsrc() {
  const ch = S.status.channels || {};
  const mode = $('chan').value;
  if (mode === 'loudest') { const f = loudest(); return f ? ch[f] : Object.values(ch)[0]; }
  if (mode === 'follow') {
    const keyed = S.status.keyed || {};
    const cur = freqOf(audio.ssrc);
    if (cur && keyed[cur] != null) { audio.lastKey = Date.now(); return audio.ssrc; }       // still talking
    if (cur && Date.now() - audio.lastKey < SILENT_MS) return audio.ssrc;                  // a short pause
    const on = Object.entries(keyed).filter(([f]) => ch[f]).sort((a, b) => b[1] - a[1]);
    if (on.length) { audio.lastKey = Date.now(); return ch[on[0][0]]; }
    return audio.ssrc || (loudest() ? ch[loudest()] : Object.values(ch)[0]);
  }
  return ch[mode];
}
function followTick() {
  if (!audio.on || $('chan').value !== 'follow') return;
  const want = wantedSsrc();
  if (want && want !== audio.ssrc) { connect(want); drawChans(); }
}
// While following, check who is on the air several times a second (the page's main poll is slower).
setInterval(async () => {
  if (!audio.on || $('chan').value !== 'follow' || !S.session) return;
  try {
    const info = await api(`/api/airband?session=${S.session}`);
    const was = JSON.stringify(S.status.keyed || {});
    S.status = info.status || {};
    followTick();
    if (JSON.stringify(S.status.keyed || {}) !== was) drawChans();
  } catch (e) { /* the main poll reports problems */ }
}, 300);
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
    const buf = audio.ctx.createBuffer(1, pcm.length, rate);
    const out = buf.getChannelData(0);
    let mean = 0; for (let i = 0; i < pcm.length; i++) mean += pcm[i]; mean /= pcm.length;   // AM: drop the carrier's DC
    for (let i = 0; i < pcm.length; i++) out[i] = (pcm[i] - mean) / 32768;
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
});
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });
$('chan').addEventListener('change', () => { if (audio.on) connect(wantedSsrc()); drawChans(); });

function drawChans() {
  const ch = S.status.channels || {};
  const keyed = S.status.keyed || {};
  const counts = {};
  for (const r of S.recs) counts[Math.round(r.freq_hz)] = (counts[Math.round(r.freq_hz)] || 0) + 1;
  const freqs = Object.keys(ch).sort();
  const prev = $('chan').value || 'follow';
  const lf = loudest();
  $('chan').innerHTML = `<option value="follow">Follow activity</option><option value="loudest">Loudest${lf ? ' · ' + mhz(lf) : ''}</option>`
    + freqs.map((f) => `<option value="${f}">${mhz(+f)}${freqInfo(+f) ? ' ' + freqInfo(+f).label : ''}</option>`).join('');
  $('chan').value = [...$('chan').options].some((o) => o.value === prev) ? prev : 'follow';
  const cur = freqOf(audio.ssrc);
  $('now').textContent = audio.on && cur ? `playing ${mhz(+cur)}${keyed[cur] != null ? ' · on air' : ''}` : '';
  $('chans').innerHTML = freqs.map((f) => `<li data-f="${f}" class="${keyed[f] != null ? 'on' : ''} ${ch[f] === audio.ssrc && audio.on ? 'sel' : ''}"
      title="${keyed[f] != null ? 'on the air now' : 'quiet'}"><b>${mhz(+f)}</b>${freqInfo(+f) ? ' ' + esc(freqInfo(+f).label) : ''} · ${counts[f] || 0}${keyed[f] != null ? ' · ON AIR' : ''}</li>`).join('')
    || '<li>no channels yet</li>';
}
$('chans').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-f]'); if (!li) return;
  $('chan').value = li.dataset.f;
  if (!audio.on) $('listen').click(); else connect(wantedSsrc());
  drawChans();
});

// ------------------------------------------------------------------ data
async function loadSession(name) {
  const info = await api('/api/airband' + (name ? `?session=${name}` : ''));
  S.session = info.session; S.recs = []; S.after = -1; S.sel = null;
  for (const id in pl) { pl[id].remove(); delete pl[id]; }
  $('session').innerHTML = info.sessions.map((s) => `<option ${s === info.session ? 'selected' : ''}>${s}</option>`).join('');
  await poll(true);
}
async function poll(full) {
  try {
    const info = await api(`/api/airband?session=${S.session}`);
    S.status = info.status || {}; S.live = info.live;
    $('fLive').className = 'live' + (S.live ? '' : ' off');
    $('fLive').lastChild.textContent = S.live ? 'live' : 'recorded';
    {
      const st = S.status, left = st.next_switch ? Math.max(0, st.next_switch - Date.now() / 1000) : null;
      const clock = left != null ? `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, '0')}` : '';
      $('fWin').textContent = !S.live ? '–' : st.listening === false
        ? `away on ${st.window} (${st.window_service})${clock ? ' · next switch in ' + clock : ''}`
        : `${st.window || ''} ${st.lo ? mhz(st.lo) + '–' + mhz(st.hi) + ' MHz' : ''}${clock ? ' · next switch in ' + clock : ''}`;
    }
    const add = await api(`/api/airband/transmissions?session=${S.session}&after=${S.after}`);
    if (add.length || full) {
      S.recs.push(...add);
      S.after = S.recs.length ? S.recs[S.recs.length - 1].i : -1;
      derive(); drawLog();
      if (S.follow && add.length) select(add[add.length - 1]);
      if (S.cursor == null) S.cursor = S.live ? Date.now() / 1000 : (S.recs.length ? S.recs[S.recs.length - 1].t1 : Date.now() / 1000);
    }
    followTick();
    drawChans();
    render();
  } catch (e) { console.warn('airband', e); }
}

$('session').addEventListener('change', () => { S.follow = true; S.cursor = null; loadSession($('session').value); });
$('scrub').addEventListener('input', () => {
  const [T0, T1] = range();
  S.follow = +$('scrub').value >= 1000;
  S.cursor = T0 + (+$('scrub').value / 1000) * (T1 - T0);
  const last = S.recs.filter((r) => r.t0 <= S.cursor).pop();
  if (last) { S.sel = last; select(last); } else render();
});
let timer = null;
$('play').addEventListener('click', () => {
  if (timer) { clearInterval(timer); timer = null; $('play').textContent = 'Play'; return; }
  const [T0, T1] = range();
  if (S.follow || S.cursor >= T1 - 1) S.cursor = T0;
  S.follow = false; $('play').textContent = 'Pause';
  timer = setInterval(() => {
    const [a, b] = range();
    S.cursor += (b - a) / 60 / 10;                     // the whole range in a minute
    const last = S.recs.filter((r) => r.t0 <= S.cursor).pop();
    if (last && last !== S.sel) select(last); else render();
    if (S.cursor >= b) { clearInterval(timer); timer = null; S.follow = true; $('play').textContent = 'Play'; }
  }, 100);
});

const lg = $('legend');
for (const k of ['check', 'alt', 'vec', 'trf', 'app', 'hand', 'other']) {
  const s = document.createElement('span');
  s.innerHTML = `<i style="background:var(${PH[k].c})"></i>${PH[k].label}`;
  lg.appendChild(s);
}
loadSession().then(() => setInterval(poll, 1500)).catch((e) => { $('dRaw').textContent = 'No airband session yet: ' + e.message; });
