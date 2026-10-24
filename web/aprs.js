// APRS, Live: stations heard on 144.390 MHz on the Bay map, with tracks, details and the raw packets.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error(r.statusText); return r.json(); };
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const hms = (t) => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`; };

const ICON = {car: '🚗', truck: '🚚', van: '🚐', jeep: '🚙', motorcycle: '🏍️', bicycle: '🚲', person: '🚶', house: '🏠',
  weather: '🌤️', digipeater: '📡', gateway: '📶', repeater: '🗼', balloon: '🎈', aircraft: '✈️', helicopter: '🚁', boat: '⛵',
  ambulance: '🚑', fire: '🚒', bus: '🚌', train: '🚆', RV: '🚐', dot: '•', other: '📍'};
const S = {session: null, stations: [], packets: [], sel: null, status: {}, live: false};

const map = L.map('map', {zoomSnap: 0.25}).setView([37.62, -122.16], 9);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 17,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
for (const ms of [200, 800, 2000]) setTimeout(() => map.invalidateSize(), ms);
new ResizeObserver(() => map.invalidateSize()).observe($('map'));
const layer = L.layerGroup().addTo(map);
map.on('zoomend', drawMap);
$('legend').innerHTML = ['car', 'house', 'weather', 'digipeater', 'gateway', 'repeater', 'person', 'balloon', 'boat', 'other']
  .map((k) => `<span>${ICON[k]} ${k}</span>`).join('') + '<span>· faded = not heard for an hour</span>';

function wxText(w) {
  if (!w) return '';
  const f = (c) => (c * 9 / 5 + 32).toFixed(0);
  return [w.temperature != null ? `${f(w.temperature)} °F` : '', w.humidity != null ? `${w.humidity}% humidity` : '',
    w.wind_speed != null ? `wind ${(w.wind_speed * 2.237).toFixed(0)} mph${w.wind_direction != null ? ' from ' + w.wind_direction + '°' : ''}` : '',
    w.pressure != null ? `${w.pressure.toFixed(1)} hPa` : '', w.rain_1h ? `rain ${w.rain_1h} mm/h` : ''].filter(Boolean).join(', ');
}
function describe(s) {
  return [s.comment, s.status, wxText(s.weather), s.speed != null ? `${(s.speed / 1.852).toFixed(0)} kn (${(s.speed * 0.621).toFixed(0)} mph)` : '',
    s.course != null ? `heading ${s.course}°` : '', s.altitude != null ? `${Math.round(s.altitude * 3.281).toLocaleString()} ft` : ''].filter(Boolean).join(' · ');
}
function drawMap() {
  layer.clearLayers();
  const now = Date.now() / 1000, z = map.getZoom();
  for (const s of S.stations) {
    if (s.lat == null) continue;
    const old = now - s.last > 3600, sel = S.sel === s.name;
    if (s.track && s.track.length > 1) L.polyline(s.track.map((p) => [p[1], p[2]]), {color: '#A3316F', weight: sel ? 3 : 1.5, opacity: old ? 0.25 : 0.6, interactive: false}).addTo(layer);
    const lab = z >= 11 || sel ? `<span class="st-lab">${esc(s.name)}</span>` : '';
    L.marker([s.lat, s.lon], {icon: L.divIcon({className: 'st-ic', iconSize: [22, 22], iconAnchor: [11, 11], html: `${ICON[s.kind] || ICON.other}${lab}`}),
                              opacity: old ? 0.45 : 1, zIndexOffset: sel ? 1000 : 0})
      .bindTooltip(`<b>${esc(s.name)}</b> · ${esc(s.kind)}${s.via ? ' (object from ' + esc(s.via) + ')' : ''}<br>${esc(describe(s))}<br>heard ${ago(s.last)} ago · ${s.packets} packets`, {direction: 'top'})
      .on('click', () => select(s.name)).addTo(layer);
  }
}
function select(name, pan) {
  const s = S.stations.find((x) => x.name === name); if (!s) return;
  S.sel = name;
  $('dTime').textContent = hms(s.last); $('dName').textContent = `${s.name} ${ICON[s.kind] || ''}`;
  $('dRaw').textContent = describe(s) || s.raw || '';
  $('dMeta').textContent = `${s.kind}${s.via ? ' · object from ' + s.via : ''} · ${s.packets} packets · first heard ${hms(s.first)}${s.path && s.path.length ? ' · via ' + s.path.join(',') : ''}${s.lat != null ? ` · ${s.lat.toFixed(4)}, ${s.lon.toFixed(4)}` : ''}`;
  if (pan && s.lat != null) map.setView([s.lat, s.lon], Math.max(map.getZoom(), 12));
  drawMap();
}
function drawList() {
  $('fSt').textContent = S.stations.length;
  $('list').innerHTML = S.stations.slice(0, 150).map((s) => `<tr data-name="${esc(s.name)}"><td>${ICON[s.kind] || ''} ${esc(s.name)}</td><td>${esc(s.kind)}</td><td>${ago(s.last)}</td><td>${s.packets}</td></tr>`).join('')
    || '<tr><td colspan="4" style="color:var(--muted)">No stations heard in this session.</td></tr>';
}
$('list').addEventListener('click', (e) => { const tr = e.target.closest('tr[data-name]'); if (tr) select(tr.dataset.name, true); });
function drawLog() {
  $('log').innerHTML = S.packets.slice(0, 300).map((p) => `<li data-name="${esc(p.object_name || p.from || '')}"><span class="t">${hms(p.t)}</span>${esc(p.raw)}${p.error ? ' <span style="color:var(--c-alt)">(not parsed)</span>' : ''}</li>`).join('')
    || '<li style="color:var(--muted)">No packets yet.</li>';
}
$('log').addEventListener('click', (e) => { const li = e.target.closest('li[data-name]'); if (li) select(li.dataset.name.trim(), true); });

async function loadSession(name) {
  const info = await api('/api/aprs' + (name ? `?session=${name}` : ''));
  S.session = info.session;
  $('session').innerHTML = info.sessions.map((s) => `<option ${s === info.session ? 'selected' : ''}>${s}</option>`).join('');
  await poll();
}
async function poll() {
  try {
    const info = await api(`/api/aprs?session=${S.session}`);
    S.status = info.status || {}; S.live = info.live;
    const st = S.status, a = st.aprs || {};
    $('fLive').className = 'live' + (S.live && st.listening ? '' : ' off');
    const left = st.next_switch ? Math.max(0, st.next_switch - Date.now() / 1000) : null;
    $('fLive').lastChild.textContent = !S.live ? 'recorded' : st.listening ? 'listening on 144.390' : `away on ${st.window}${left != null ? ` · switch in ${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, '0')}` : ''}`;
    const notice = !S.live ? 'No APRS session is running. Start voice_session.py with an aprs window (see README).'
      : a.error ? a.error : !st.listening ? 'The receiver is on another window right now; APRS is decoded during its turn in the rotation. “Switch receiver here” keeps it on 144.390 MHz.' : '';
    $('notice').textContent = notice; $('notice').style.display = notice ? '' : 'none';
    const [stn, pk] = await Promise.all([api(`/api/aprs/stations?session=${S.session}`), api(`/api/aprs/packets?session=${S.session}&limit=300`)]);
    S.stations = stn.stations || []; S.packets = pk;
    $('fPk').textContent = stn.packets ?? S.packets.length;
    drawMap(); drawList(); drawLog();
  } catch (e) { $('notice').textContent = `No APRS session yet (${e.message}).`; $('notice').style.display = ''; }
}
$('session').addEventListener('change', () => loadSession($('session').value));
loadSession().catch(() => poll()).finally(() => setInterval(poll, 4000));
