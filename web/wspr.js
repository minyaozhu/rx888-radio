// WSPR Propagation: beacons heard (great-circle lines from home), band conditions from WSPR + FT8 + FT4.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p, o) => { const r = await fetch(p, o); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); };
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const utc = (t) => new Date(t * 1000).toISOString().slice(11, 16);
const BANDS = ['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m', '12m', '10m', '6m'];
const COLORS = ['#8C564B', '#D62728', '#FF7F0E', '#BCBD22', '#2CA02C', '#17BECF', '#1F77B4', '#9467BD', '#E377C2', '#7F7F7F', '#4C4CA8'];
const bandColor = (b) => COLORS[Math.max(0, BANDS.indexOf(b))];

function gridLL(g) {
  if (!g || !/^[A-R]{2}\d{2}([A-X]{2})?$/i.test(g)) return null;
  g = g.toUpperCase();
  let lon = (g.charCodeAt(0) - 65) * 20 - 180 + (+g[2]) * 2, lat = (g.charCodeAt(1) - 65) * 10 - 90 + (+g[3]);
  if (g.length >= 6) { lon += (g.charCodeAt(4) - 65) * (2 / 24) + 1 / 24; lat += (g.charCodeAt(5) - 65) / 24 + 1 / 48; } else { lon += 1; lat += 0.5; }
  return [lat, lon];
}
// great circle from a to b as a list of points (so long paths curve like they should)
function arc(a, b, n = 48) {
  const r = Math.PI / 180, [la1, lo1, la2, lo2] = [a[0] * r, a[1] * r, b[0] * r, b[1] * r];
  const d = 2 * Math.asin(Math.sqrt(Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2));
  if (d < 1e-6) return [a, b];
  const pts = [];
  let prevLon = null;
  for (let i = 0; i <= n; i++) {
    const f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
    const y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
    const z = A * Math.sin(la1) + B * Math.sin(la2);
    let lon = Math.atan2(y, x) / r;
    if (prevLon !== null) while (lon - prevLon > 180) lon -= 360; while (prevLon !== null && lon - prevLon < -180) lon += 360;
    prevLon = lon;
    pts.push([Math.atan2(z, Math.sqrt(x * x + y * y)) / r, lon]);
  }
  return pts;
}

const map = L.map('map', {worldCopyJump: true, zoomSnap: 0.5}).setView([30, -100], 2.5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 10,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
for (const ms of [200, 800]) setTimeout(() => map.invalidateSize(), ms);
const layer = L.layerGroup().addTo(map);
$('legend').innerHTML = BANDS.map((b) => `<span><i style="background:${bandColor(b)}"></i>${b}</span>`).join('') + '<span>· line width = signal strength</span>';

const S = {spots: [], info: null, home: null};

function draw() {
  layer.clearLayers();
  const cut = Date.now() / 1000 - +$('hours').value * 3600, band = $('band').value;
  const spots = S.spots.filter((s) => s.t > cut && (!band || s.band === band));
  if (S.home) L.circleMarker(S.home, {radius: 7, color: '#fff', weight: 2, fillColor: '#C2410C', fillOpacity: 1}).bindTooltip(`Home · ${esc(S.info.qth)}`).addTo(layer);
  const best = new Map();                                  // one line per station and band: its best SNR
  for (const s of spots) { const k = s.call + '|' + s.band; if (!best.has(k) || s.snr > best.get(k).snr) best.set(k, s); }
  for (const s of best.values()) {
    const p = gridLL(s.grid); if (!p) continue;
    if (S.home) L.polyline(arc(S.home, p), {color: bandColor(s.band), weight: Math.max(1, Math.min(4, (s.snr + 30) / 7)), opacity: 0.7, interactive: false}).addTo(layer);
    L.circleMarker(p, {radius: 4, color: bandColor(s.band), weight: 1, fillColor: bandColor(s.band), fillOpacity: 0.9})
      .bindTooltip(`<b>${esc(s.call)}</b> ${esc(s.grid)} · ${s.band} · SNR ${s.snr} dB · ${s.dbm != null ? s.dbm + ' dBm' : ''}${s.km ? ' · ' + Math.round(s.km).toLocaleString() + ' km' : ''}<br>${utc(s.t)} UTC`).addTo(layer);
  }
  const calls = new Set(spots.map((s) => s.call));
  $('fSpots').textContent = spots.length; $('fSt').textContent = calls.size;
  const far = spots.filter((s) => s.km).sort((a, b) => b.km - a.km)[0];
  $('fFar').textContent = far ? `${far.call} · ${Math.round(far.km).toLocaleString()} km (${far.band})` : '–';
  $('spots').innerHTML = spots.slice(0, 200).map((s) => `<tr data-grid="${esc(s.grid || '')}"><td>${utc(s.t)}</td><td style="color:${bandColor(s.band)}">${s.band}</td>
    <td>${esc(s.call)}</td><td>${esc(s.grid || '')}</td><td>${s.snr}</td><td>${s.km ? Math.round(s.km).toLocaleString() : ''}</td></tr>`).join('')
    || '<tr><td colspan="6" class="shut">No spots in this period.</td></tr>';
}
$('spots').addEventListener('click', (e) => { const tr = e.target.closest('tr[data-grid]'); const p = tr && gridLL(tr.dataset.grid); if (p) map.setView(p, Math.max(map.getZoom(), 4)); });
$('hours').addEventListener('change', draw);
$('band').addEventListener('change', draw);

async function conditions() {
  try {
    const p = await api('/api/propagation?minutes=30');
    const rows = BANDS.map((b) => p.bands.find((x) => x.band === b) || {band: b, wspr: 0, ft8: 0, ft4: 0, stations: 0, far_km: 0});
    $('cond').innerHTML = rows.map((x) => { const open = x.wspr + x.ft8 + x.ft4 > 0;
      return `<tr><td class="${open ? 'open' : 'shut'}" style="color:${open ? bandColor(x.band) : ''}">${x.band}${open ? ' ●' : ''}</td><td>${x.wspr || ''}</td><td>${x.ft8 || ''}</td><td>${x.ft4 || ''}</td>
        <td>${x.stations || ''}</td><td>${x.far_km ? Math.round(x.far_km).toLocaleString() + ' km' + (x.far_call ? ' ' + esc(x.far_call) : '') : ''}</td></tr>`; }).join('');
  } catch (e) { /* rxapp restarting */ }
}

function drawMonitor() {
  const i = S.info; if (!i) return;
  $('fMon').className = 'live' + (i.running ? '' : ' off');
  $('fMon').lastChild.textContent = i.running ? `on · ${i.bands.filter((b) => !b.parked).length}/${i.bands.length} bands hearing` : 'stopped';
  $('monBtn').textContent = i.running ? 'Stop WSPR monitor' : 'Start WSPR monitor';
  const notice = !i.decoder ? 'wsprd is not built (see README, WSPR).'
    : !i.running ? 'The WSPR monitor is stopped. Start it to decode beacons every 2 minutes on 11 bands.'
    : i.bands.every((b) => b.parked) ? 'The receiver is on VHF right now, so WSPR is paused; it decodes whenever the HF input is selected. “Switch receiver here” keeps it there.'
    : !S.spots.length ? 'Listening. Spots appear 2 minutes at a time (each beacon transmits for 110 s from an even minute).' : '';
  $('notice').textContent = notice; $('notice').style.display = notice ? '' : 'none';
}
$('monBtn').addEventListener('click', async () => {
  $('monBtn').disabled = true;
  try { S.info = await api('/api/wspr', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({on: !S.info.running})}); }
  catch (e) { alert(e.message); }
  $('monBtn').disabled = false; drawMonitor();
});

async function refresh() {
  try {
    S.info = await api('/api/wspr');
    S.home = gridLL(S.info.qth);
    S.spots = await api(`/api/wspr/spots?since=${Date.now() / 1000 - 24 * 3600}&limit=20000`);
    const bands = [...new Set(S.spots.map((s) => s.band))].sort((a, b) => BANDS.indexOf(a) - BANDS.indexOf(b));
    const prev = $('band').value;
    $('band').innerHTML = '<option value="">all</option>' + bands.map((b) => `<option>${b}</option>`).join('');
    $('band').value = bands.includes(prev) ? prev : '';
    drawMonitor(); draw(); conditions();
  } catch (e) { $('notice').textContent = 'rxapp not reachable: ' + e.message; $('notice').style.display = ''; }
}
refresh(); setInterval(refresh, 20000);
