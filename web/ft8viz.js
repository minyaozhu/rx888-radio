// FT8 Skywatch: world map + per-station timeline of rxapp's FT8 decodes, live or replayed.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({detail: r.statusText}))).detail);
  return r.json();
};
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const utc = (t) => new Date(t * 1000).toISOString().slice(11, 19);
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);

// ------------------------------------------------------------------ message phases
const PHASES = {
  cq: {label: 'CQ', v: '--p-cq'},
  grid: {label: 'Answer + grid', v: '--p-grid'},
  rep: {label: 'Signal report', v: '--p-rep'},
  rrep: {label: 'Roger + report', v: '--p-rrep'},
  end: {label: 'RR73 / 73', v: '--p-end'},
  other: {label: 'Other', v: '--p-other'},
};
const GRID = /^[A-R]{2}\d{2}$/;
function phase(r) {
  if (r.to_call === 'CQ') return 'cq';
  const e = r.extra || '';
  if (!r.from_call) return 'other';
  if (GRID.test(e) && e !== 'RR73') return 'grid';
  if (/^R[-+]\d+$/.test(e)) return 'rrep';
  if (/^[-+]\d+$/.test(e)) return 'rep';
  if (/^(RR73|RRR|73)$/.test(e)) return 'end';
  return 'other';
}
const BAND_ORDER = ['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m', '12m', '10m', '6m'];
const BAND_COLORS = ['#8C564B', '#D62728', '#FF7F0E', '#BCBD22', '#2CA02C', '#17BECF', '#1F77B4', '#9467BD', '#E377C2', '#7F7F7F', '#4C4CA8'];
const bandColor = (b) => BAND_COLORS[Math.max(0, BAND_ORDER.indexOf(b))];

// ------------------------------------------------------------------ geography
function gridLL(g) {
  if (!g || g.length < 4) return null;
  g = g.toUpperCase();
  let lon = (g.charCodeAt(0) - 65) * 20 - 180 + (+g[2]) * 2;
  let lat = (g.charCodeAt(1) - 65) * 10 - 90 + (+g[3]);
  if (g.length >= 6) { lon += (g.charCodeAt(4) - 65) * (2 / 24) + 1 / 24; lat += (g.charCodeAt(5) - 65) / 24 + 1 / 48; }
  else { lon += 1; lat += 0.5; }
  return [lat, lon];
}
function km([la1, lo1], [la2, lo2]) {
  const r = Math.PI / 180, a = Math.sin((la2 - la1) * r / 2) ** 2 +
    Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin((lo2 - lo1) * r / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.sqrt(a));
}
// Stations placed by prefix get a small stable offset so a country's stations don't stack exactly
function jitter(call) {
  let h = 2166136261;
  for (const c of call) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return [((h & 0xFF) / 255 - 0.5) * 2.4, (((h >> 8) & 0xFF) / 255 - 0.5) * 3.2];
}

// ------------------------------------------------------------------ state
const S = {
  all: [], lastId: 0, win: 3600, band: '', find: '', qth: 'CM87', home: null,
  cursor: null, follow: true, playing: false, sel: null, rows: [], stations: new Map(), lanes: [],
};

function derive() {
  const rows = S.band ? S.all.filter((r) => r.band === S.band) : S.all;
  S.rows = rows;
  for (const r of rows) r.ph ??= phase(r);
  const st = new Map();
  const get = (call) => {
    let s = st.get(call);
    if (!s) {
      const p = prefixInfo(call);
      s = {call, grid: null, country: p ? p.name : null, pre: p, n: 0, first: Infinity, last: 0, bands: new Map(), rows: []};
      st.set(call, s);
    }
    return s;
  };
  for (const r of rows) {
    if (r.from_call && !r.from_call.startsWith('<')) {
      const s = get(r.from_call);
      s.n++; s.first = Math.min(s.first, r.t); s.last = Math.max(s.last, r.t);
      s.bands.set(r.band, (s.bands.get(r.band) || 0) + 1);
      s.rows.push(r);
      if (r.ph === 'grid' || r.ph === 'cq') { const g = (r.extra || '').split(' ')[0]; if (GRID.test(g) && g !== 'RR73') s.grid = g; }
    }
    if (r.to_call && r.to_call !== 'CQ' && !r.to_call.startsWith('<')) get(r.to_call);   // placed, even if never heard
  }
  for (const s of st.values()) {
    const g = gridLL(s.grid);
    if (g) { s.ll = g; s.approx = false; }
    else if (s.pre) { const [a, b] = jitter(s.call); s.ll = [s.pre.lat + a, s.pre.lon + b]; s.approx = true; }
    else s.ll = null;
    s.dist = s.ll && S.home ? km(S.home, s.ll) : null;
    s.band = [...s.bands.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  }
  S.stations = st;
  // Timeline lanes: stations we heard, in order of first appearance (busiest 80 if there are more)
  let heard = [...st.values()].filter((s) => s.n > 0);
  if (S.find) heard = heard.filter((s) => s.call.includes(S.find) || s.rows.some((r) => r.msg.includes(S.find)));
  if (heard.length > 80) heard = heard.sort((a, b) => b.n - a.n).slice(0, 80);
  S.lanes = heard.sort((a, b) => a.first - b.first);
  S.laneOf = new Map(S.lanes.map((s, i) => [s.call, i]));
  renderFacts();
  renderLists();
}

// ------------------------------------------------------------------ facts and lists
function renderFacts() {
  const t1 = Date.now() / 1000, t0 = t1 - S.win;
  $('fWin').textContent = `${S.win >= 3600 ? S.win / 3600 + ' h' : S.win / 60 + ' min'} · ${utc(t0).slice(0, 5)}–${utc(t1).slice(0, 5)} UTC`;
  $('fMsgs').textContent = S.rows.length.toLocaleString();
  const heard = [...S.stations.values()].filter((s) => s.n > 0);
  $('fStations').textContent = heard.length;
  $('fCountries').textContent = new Set(heard.map((s) => s.country).filter(Boolean)).size;
  const done = new Set(S.rows.filter((r) => r.ph === 'end' && r.to_call).map((r) => [r.from_call, r.to_call].sort().join(' ')));
  $('fQsos').textContent = done.size;
  const far = heard.filter((s) => s.dist != null && !s.approx).sort((a, b) => b.dist - a.dist)[0];
  $('fFar').textContent = far ? `${far.call} · ${Math.round(far.dist).toLocaleString()} km` : '–';
}

function renderLists() {
  const heard = [...S.stations.values()].filter((s) => s.n > 0 && s.dist != null);
  const far = heard.sort((a, b) => (a.approx - b.approx) || b.dist - a.dist).slice(0, 8);
  $('farList').innerHTML = far.map((s) => `<li data-call="${esc(s.call)}"><span>${esc(s.call)} <span style="color:var(--muted)">${esc(s.country || '')}</span></span>
    <span>${s.approx ? '~' : ''}${Math.round(s.dist).toLocaleString()} km</span></li>`).join('') || '<li><span class="meta">nothing yet</span></li>';
  const byBand = new Map();
  for (const r of S.all) {
    const b = byBand.get(r.band) || {n: 0, calls: new Set()};
    b.n++; if (r.from_call) b.calls.add(r.from_call);
    byBand.set(r.band, b);
  }
  $('bandList').innerHTML = BAND_ORDER.filter((b) => byBand.has(b)).map((b) => `<li data-band="${b}">
    <span><i style="display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px;background:${bandColor(b)}"></i>${b}${S.band === b ? ' ✓' : ''}</span>
    <span>${byBand.get(b).n} msgs · ${byBand.get(b).calls.size} stations</span></li>`).join('');
}

// ------------------------------------------------------------------ map
const NS = 'http://www.w3.org/2000/svg';
const M = {};
async function initMap() {
  const svg = d3.select('#map');
  M.proj = d3.geoNaturalEarth1().fitExtent([[6, 6], [954, 494]], {type: 'Sphere'});
  M.path = d3.geoPath(M.proj);
  M.root = svg.append('g');
  M.root.append('path').datum({type: 'Sphere'}).attr('d', M.path).attr('fill', 'var(--water)');
  M.root.append('path').datum(d3.geoGraticule10()).attr('class', 'grat').attr('d', M.path);
  M.landG = M.root.append('g');
  M.arcs = M.root.append('g');
  M.rings = M.root.append('g');
  M.stns = M.root.append('g');
  M.home = M.root.append('g');
  M.tags = M.root.append('g');
  M.k = 1;
  const zoom = d3.zoom().scaleExtent([1, 14]).translateExtent([[0, 0], [960, 500]])
    .on('zoom', (e) => { M.k = e.transform.k; M.root.attr('transform', e.transform); renderMap(); });
  svg.call(zoom).on('dblclick.zoom', null);
  svg.on('dblclick', () => svg.transition().duration(400).call(zoom.transform, d3.zoomIdentity));
  try {
    const world = await d3.json('https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json');
    M.landG.selectAll('path').data(topojson.feature(world, world.objects.countries).features)
      .join('path').attr('class', 'land').attr('d', M.path);
  } catch (e) { console.warn('world map', e); }
}

function renderMap() {
  if (!M.proj) return;
  const k = M.k, cur = S.cursor;
  const trail = Math.min(S.win, 1800);              // stations fade over 30 min (or the window)
  // last time each station was heard at or before the cursor
  const lastSeen = new Map();
  for (const r of S.rows) {
    if (r.t > cur) break;
    if (r.from_call) lastSeen.set(r.from_call, r.t);
  }
  const pts = [];
  for (const s of S.stations.values()) {
    const t = lastSeen.get(s.call);
    if (t == null || !s.ll) continue;
    const xy = M.proj([s.ll[1], s.ll[0]]);
    const age = cur - t;
    pts.push({s, xy, op: age <= 60 ? 1 : Math.max(0.22, 1 - age / trail)});
  }
  pts.sort((a, b) => a.op - b.op);
  M.stns.selectAll('circle').data(pts, (d) => d.s.call).join('circle')
    .attr('class', (d) => `stn${d.s.approx ? ' approx' : ''}${S.sel && S.sel.from_call === d.s.call ? ' sel' : ''}`)
    .attr('cx', (d) => d.xy[0]).attr('cy', (d) => d.xy[1])
    .attr('r', (d) => (3 + Math.min(3, Math.log2(1 + d.s.n) * 0.8)) / Math.sqrt(k))
    .attr('fill', (d) => bandColor(d.s.band)).attr('stroke', (d) => bandColor(d.s.band))
    .attr('opacity', (d) => d.op)
    .on('click', (e, d) => selectStation(d.s.call))
    .selectAll('title').data((d) => [d]).join('title')
    .text((d) => `${d.s.call}${d.s.grid ? ' · ' + d.s.grid : ''}${d.s.country ? ' · ' + d.s.country : ''}${d.s.dist != null ? ' · ' + (d.s.approx ? '~' : '') + Math.round(d.s.dist) + ' km' : ''}`);

  // the last two slots before the cursor: who is calling whom, and who is calling CQ
  const recent = [];
  for (let i = S.rows.length - 1; i >= 0; i--) {
    const r = S.rows[i];
    if (r.t > cur) continue;
    if (r.t <= cur - 30) break;
    recent.push(r);
  }
  const arcs = recent.filter((r) => r.ph !== 'cq' && r.to_call && S.stations.get(r.from_call)?.ll && S.stations.get(r.to_call)?.ll);
  M.arcs.selectAll('path').data(arcs, (r) => r.id).join('path').attr('class', 'arc')
    .attr('d', (r) => M.path({type: 'LineString', coordinates: [
      [S.stations.get(r.from_call).ll[1], S.stations.get(r.from_call).ll[0]],
      [S.stations.get(r.to_call).ll[1], S.stations.get(r.to_call).ll[0]]]}))
    .attr('stroke', (r) => css(PHASES[r.ph].v)).attr('opacity', (r) => r === S.sel ? 1 : 0.75)
    .attr('stroke-width', (r) => r === S.sel ? 3 : 1.6);
  const cqs = recent.filter((r) => r.ph === 'cq' && S.stations.get(r.from_call)?.ll);
  M.rings.selectAll('circle').data(cqs, (r) => r.id).join('circle')
    .attr('cx', (r) => M.proj([S.stations.get(r.from_call).ll[1], S.stations.get(r.from_call).ll[0]])[0])
    .attr('cy', (r) => M.proj([S.stations.get(r.from_call).ll[1], S.stations.get(r.from_call).ll[0]])[1])
    .attr('r', 9 / Math.sqrt(k)).attr('fill', 'none').attr('stroke', css('--p-cq')).attr('stroke-width', 1.4)
    .attr('vector-effect', 'non-scaling-stroke').attr('opacity', 0.85);

  // home
  if (S.home) {
    const [x, y] = M.proj([S.home[1], S.home[0]]);
    const r = 7 / Math.sqrt(k);
    const star = d3.range(10).map((i) => {
      const a = Math.PI / 5 * i - Math.PI / 2, rr = i % 2 ? r * 0.45 : r;
      return [x + rr * Math.cos(a), y + rr * Math.sin(a)];
    });
    M.home.selectAll('path').data([star]).join('path').attr('class', 'homestar').attr('d', (d) => 'M' + d.join('L') + 'Z')
      .selectAll('title').data([0]).join('title').text(`Home · ${S.qth}`);
  }
  // labels: the selected message's stations, plus the ends of the newest arcs
  const lab = new Map();
  if (S.sel) for (const c of [S.sel.from_call, S.sel.to_call]) if (S.stations.get(c)?.ll) lab.set(c, S.stations.get(c));
  for (const r of arcs.slice(0, 6)) for (const c of [r.from_call, r.to_call]) if (lab.size < 10) lab.set(c, S.stations.get(c));
  M.tags.selectAll('text').data([...lab.values()], (s) => s.call).join('text').attr('class', 'maptag')
    .attr('x', (s) => M.proj([s.ll[1], s.ll[0]])[0] + 6 / Math.sqrt(k))
    .attr('y', (s) => M.proj([s.ll[1], s.ll[0]])[1] - 6 / Math.sqrt(k))
    .attr('font-size', 11 / Math.sqrt(k)).attr('stroke-width', 3 / Math.sqrt(k)).text((s) => s.call);
}

// ------------------------------------------------------------------ timeline (canvas)
const TL = {left: 168, top: 26, lane: 18, right: 14};
// The axis spans the window, or from the first decode if there's less data than that (at least 15 min)
function tRange() {
  const t1 = Date.now() / 1000;
  const first = S.all.length ? S.all[0].t - 60 : t1 - S.win;
  return [Math.max(t1 - S.win, Math.min(first, t1 - 900)), t1];
}

function renderTimeline() {
  const cv = $('timeline'), wrap = $('tlwrap');
  const w = Math.max(700, wrap.clientWidth), h = TL.top + Math.max(1, S.lanes.length) * TL.lane + 8;
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== w * dpr || cv.height !== h * dpr) {
    cv.width = w * dpr; cv.height = h * dpr; cv.style.width = w + 'px'; cv.style.height = h + 'px';
  }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const [t0, t1] = tRange();
  const x = (t) => TL.left + (t - t0) / (t1 - t0) * (w - TL.left - TL.right);
  TL.x = x; TL.t0 = t0; TL.t1 = t1; TL.w = w;
  const ink = css('--ink'), muted = css('--muted'), line = css('--line');
  // axis
  g.font = '400 11px "IBM Plex Mono", monospace'; g.fillStyle = muted; g.strokeStyle = line; g.lineWidth = 1;
  const ticks = d3.scaleUtc().domain([new Date(t0 * 1000), new Date(t1 * 1000)]).ticks(Math.floor((w - TL.left) / 90));
  for (const d of ticks) {
    const xx = Math.round(x(d / 1000)) + 0.5;
    g.beginPath(); g.moveTo(xx, TL.top - 6); g.lineTo(xx, h); g.globalAlpha = 0.5; g.stroke(); g.globalAlpha = 1;
    g.fillText(d.toISOString().slice(11, 16), xx - 16, 14);
  }
  g.fillText('UTC', 8, 14);
  if (!S.lanes.length) { g.fillStyle = muted; g.fillText('No messages in this window yet.', TL.left, TL.top + 14); return; }
  // lanes
  S.lanes.forEach((s, i) => {
    const y = TL.top + i * TL.lane;
    if (i % 2) { g.fillStyle = line; g.globalAlpha = 0.18; g.fillRect(0, y, w, TL.lane); g.globalAlpha = 1; }
    g.fillStyle = S.sel && S.sel.from_call === s.call ? css('--magenta') : ink;
    g.font = '600 13px "Barlow Condensed", sans-serif';
    g.fillText(s.call, 8, y + 13);
    g.fillStyle = muted; g.font = '400 10px "IBM Plex Mono", monospace';
    const sub = [s.grid || (s.country ? s.country.slice(0, 12) : ''), s.dist != null ? (s.approx ? '~' : '') + Math.round(s.dist) + 'km' : ''].filter(Boolean).join(' ');
    g.fillText(sub, 74, y + 12);
  });
  // dots
  const colors = Object.fromEntries(Object.entries(PHASES).map(([k, p]) => [k, css(p.v)]));
  for (const r of S.rows) {
    const i = S.laneOf.get(r.from_call);
    if (i == null || r.t < t0) continue;
    const cx = x(r.t + 7.5), cy = TL.top + i * TL.lane + TL.lane / 2;
    g.globalAlpha = r.t > S.cursor ? 0.18 : 1;
    g.fillStyle = colors[r.ph];
    g.beginPath(); g.arc(cx, cy, 3.6, 0, 2 * Math.PI); g.fill();
  }
  g.globalAlpha = 1;
  if (S.sel && S.laneOf.has(S.sel.from_call)) {
    const cx = x(S.sel.t + 7.5), cy = TL.top + S.laneOf.get(S.sel.from_call) * TL.lane + TL.lane / 2;
    g.strokeStyle = ink; g.lineWidth = 2; g.beginPath(); g.arc(cx, cy, 6, 0, 2 * Math.PI); g.stroke();
  }
  // cursor
  const cx = Math.round(x(S.cursor)) + 0.5;
  g.strokeStyle = css('--magenta'); g.lineWidth = 1.5;
  g.beginPath(); g.moveTo(cx, TL.top - 8); g.lineTo(cx, h); g.stroke();
}

function timelineHit(e) {
  const rect = $('timeline').getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  if (px < TL.left) {                                   // lane label: that station's latest message
    const i = Math.floor((py - TL.top) / TL.lane);
    if (S.lanes[i]) selectStation(S.lanes[i].call);
    return;
  }
  const i = Math.floor((py - TL.top) / TL.lane);
  const s = S.lanes[i];
  if (!s) return;
  let best = null, bd = 9;
  for (const r of s.rows) { const d = Math.abs(TL.x(r.t + 7.5) - px); if (d < bd) { bd = d; best = r; } }
  if (best) select(best, true);
  else setCursor(TL.t0 + (px - TL.left) / (TL.w - TL.left - TL.right) * (TL.t1 - TL.t0));
}

// ------------------------------------------------------------------ selection and detail
function meaning(r) {
  const f = r.from_call, t = r.to_call, e = r.extra || '';
  if (r.ph === 'cq') {
    const w = r.msg.split(' ')[1];
    const tag = w && w !== f && !/\d/.test(w) ? w : null;
    const want = tag === 'DX' ? 'looking for distant stations' : tag === 'POTA' ? 'operating from a park (Parks on the Air), looking for contacts'
      : tag === 'SOTA' ? 'operating from a summit, looking for contacts' : tag ? `looking for ${tag} stations` : 'looking for anyone to answer';
    return `${f} is calling CQ${tag ? ' ' + tag : ''}${GRID.test(e.split(' ').pop()) ? ' from grid ' + e.split(' ').pop() : ''}: ${want}.`;
  }
  if (!f) return 'A message in a non-standard format: free text, a contest exchange, or a telemetry message.';
  const ff = f.startsWith('<') ? 'A station sent as a hash' : f, tt = t && t.startsWith('<') ? 'a station sent as a hash' : t;
  switch (r.ph) {
    case 'grid': return `${ff} answers ${tt} and gives its location, grid ${e}.`;
    case 'rep': return `${ff} reports ${tt}'s signal at ${e} dB (how strong ${tt} is heard at ${ff}).`;
    case 'rrep': return `${ff} acknowledges ${tt}'s report ("R") and reports ${tt}'s signal at ${e.slice(1)} dB.`;
    case 'end':
      if (e === 'RR73') return `${ff} confirms everything and signs off with ${tt}: the contact is complete.`;
      if (e === 'RRR') return `${ff} confirms everything ("Roger") to ${tt}.`;
      return `${ff} sends 73 (best regards) to ${tt}: the exchange is over.`;
    default: return `${ff} to ${tt}: ${e}`;
  }
}

function select(r, moveCursor) {
  S.sel = r;
  if (moveCursor && r.t + 15 < Date.now() / 1000 - 1) { S.follow = false; S.cursor = r.t + 14.9; }
  const p = PHASES[r.ph];
  $('dTime').textContent = utc(r.t) + ' UTC';
  $('dCall').textContent = r.ph === 'cq' ? `${r.from_call} · CQ` : r.from_call ? `${r.from_call} → ${r.to_call}` : r.band;
  $('dPhase').textContent = p.label; $('dPhase').style.color = css(p.v);
  $('dRaw').textContent = r.msg;
  $('dMeaning').textContent = meaning(r);
  const s = S.stations.get(r.from_call);
  const where = s ? [s.country, s.grid ? 'grid ' + s.grid : s.approx ? 'position from callsign (approximate)' : null,
    s.dist != null ? `${s.approx ? '~' : ''}${Math.round(s.dist).toLocaleString()} km from ${S.qth}` : null].filter(Boolean).join(' · ') : '';
  $('dMeta').innerHTML = `${r.band} · ${((r.dial_hz + r.hz) / 1e3).toFixed(3)} kHz · SNR ${r.score.toFixed(0)} · dt ${r.dt.toFixed(1)} s${where ? '<br>' + esc(where) : ''}`;
  draw();
}

function selectStation(call) {
  const s = S.stations.get(call);
  if (!s || !s.rows.length) return;
  const before = s.rows.filter((r) => r.t <= S.cursor);
  select(before.length ? before[before.length - 1] : s.rows[s.rows.length - 1], !before.length);
}

// ------------------------------------------------------------------ time controls
function setCursor(t, keepFollow) {
  const [t0, t1] = tRange();
  S.cursor = Math.max(t0, Math.min(t1, t));
  if (!keepFollow) S.follow = S.cursor >= t1 - 0.5;
  draw();
}
function syncControls() {
  const [t0, t1] = tRange();
  $('scrub').value = Math.round((S.cursor - t0) / (t1 - t0) * 1000);
  $('clock').innerHTML = utc(S.cursor) + ' UTC' + (S.follow ? ' <span class="live">● live</span>' : '');
  $('play').textContent = S.playing ? 'Pause' : 'Play';
  $('play').setAttribute('aria-pressed', S.playing);
}
let raf = 0;
function draw() {
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(() => { syncControls(); renderMap(); renderTimeline(); });
}

let lastFrame = 0;
function playStep(ts) {
  if (!S.playing) return;
  const dt = lastFrame ? (ts - lastFrame) / 1000 : 0;
  lastFrame = ts;
  const speed = S.win / 60;                              // the whole window in a minute
  const [, t1] = tRange();
  S.cursor += dt * speed;
  if (S.cursor >= t1) { S.cursor = t1; S.playing = false; S.follow = true; }
  syncControls(); renderMap(); renderTimeline();
  if (S.playing) requestAnimationFrame(playStep);
}

// ------------------------------------------------------------------ data
async function load() {
  const info = await api('/api/ft8');
  S.qth = info.qth || 'CM87';
  S.home = gridLL(S.qth);
  $('qth').value = S.qth;
  const since = Date.now() / 1000 - S.win;
  const rows = await api(`/api/ft8/decodes?since=${since}&limit=50000`);
  S.all = rows.reverse();
  S.lastId = S.all.reduce((m, r) => Math.max(m, r.id), 0);
  const bands = BAND_ORDER.filter((b) => info.available.includes(b));
  $('band').innerHTML = '<option value="">all</option>' + bands.map((b) => `<option ${b === S.band ? 'selected' : ''}>${b}</option>`).join('');
  if (S.follow || S.cursor == null) S.cursor = Date.now() / 1000;
  derive(); draw();
}

async function poll() {
  try {
    const add = await api(`/api/ft8/decodes?after_id=${S.lastId}&limit=50000`);
    if (add.length) {
      add.reverse();
      S.all.push(...add);
      S.all.sort((a, b) => a.t - b.t || a.id - b.id);
      S.lastId = Math.max(S.lastId, ...add.map((r) => r.id));
    }
    const cut = Date.now() / 1000 - S.win - 60;
    if (S.all.length && S.all[0].t < cut) S.all = S.all.filter((r) => r.t >= cut);
    if (S.follow && !S.playing) S.cursor = Date.now() / 1000;
    if (add.length) derive();
    if (!S.playing) draw();
  } catch (e) { console.warn('poll', e); }
}

// ------------------------------------------------------------------ wiring
function legend() {
  $('legend').innerHTML = '<span>Timeline:</span>' + Object.values(PHASES).map((p) => `<span><i style="background:var(${p.v})"></i>${p.label}</span>`).join('')
    + '<span style="margin-left:14px">Map: colour = band · hollow = placed by callsign · <span style="color:var(--home)">★</span> home</span>';
}

async function main() {
  legend();
  await initMap();
  $('scrub').addEventListener('input', () => {
    const [t0, t1] = tRange();
    S.playing = false;
    setCursor(t0 + (+$('scrub').value / 1000) * (t1 - t0));
  });
  $('play').addEventListener('click', () => {
    S.playing = !S.playing;
    if (S.playing) {
      const [t0, t1] = tRange();
      if (S.follow || S.cursor >= t1 - 16) S.cursor = t0;  // at the live end: replay from the start
      S.follow = false; lastFrame = 0; requestAnimationFrame(playStep);
    }
    syncControls();
  });
  $('win').addEventListener('change', () => { S.win = +$('win').value; load(); });
  $('band').addEventListener('change', () => { S.band = $('band').value; derive(); draw(); });
  $('find').addEventListener('input', () => { S.find = $('find').value.trim().toUpperCase(); derive(); draw(); });
  $('qth').addEventListener('change', async () => {
    try { await api('/api/ft8/qth', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({grid: $('qth').value})}); }
    catch (e) { alert(e.message); }
    load();
  });
  $('timeline').addEventListener('click', timelineHit);
  $('farList').addEventListener('click', (e) => { const li = e.target.closest('li[data-call]'); if (li) selectStation(li.dataset.call); });
  $('bandList').addEventListener('click', (e) => {
    const li = e.target.closest('li[data-band]'); if (!li) return;
    S.band = S.band === li.dataset.band ? '' : li.dataset.band; $('band').value = S.band; derive(); draw();
  });
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === ' ') { e.preventDefault(); $('play').click(); }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault(); S.playing = false; setCursor(S.cursor + (e.key === 'ArrowRight' ? 15 : -15) * (e.shiftKey ? 20 : 1));
    }
  });
  new ResizeObserver(() => draw()).observe($('tlwrap'));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => draw());
  await load();
  setInterval(poll, 5000);
}
main();
