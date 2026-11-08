// SPDX-License-Identifier: GPL-3.0-or-later
// WSPR Propagation: beacons heard (great-circle lines from home), band conditions from WSPR + FT8 + FT4 + CW.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p, o) => { const r = await fetch(p, o); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); };
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const utc = (t) => new Date(t * 1000).toISOString().slice(11, 16);
const BANDS = ['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m', '12m', '10m', '6m'];
// bandColor, worldMap: worldmap.js (the FT8 page's map)

function gridLL(g) {
  if (!g || !/^[A-R]{2}\d{2}([A-X]{2})?$/i.test(g)) return null;
  g = g.toUpperCase();
  let lon = (g.charCodeAt(0) - 65) * 20 - 180 + (+g[2]) * 2, lat = (g.charCodeAt(1) - 65) * 10 - 90 + (+g[3]);
  if (g.length >= 6) { lon += (g.charCodeAt(4) - 65) * (2 / 24) + 1 / 24; lat += (g.charCodeAt(5) - 65) / 24 + 1 / 48; } else { lon += 1; lat += 0.5; }
  return [lat, lon];
}

const S = {spots: [], info: null, home: null, sel: null};
const map = worldMap('#map', {onPick: (id) => { S.sel = S.sel === id ? null : id; draw(); }});
$('legend').innerHTML = BANDS.map((b) => `<span><i style="background:${bandColor(b)}"></i>${b}</span>`).join('') + '<span>· line width = signal strength</span>';

function draw() {
  const cut = Date.now() / 1000 - +$('hours').value * 3600, band = $('band').value;
  const spots = S.spots.filter((s) => s.t > cut && (!band || s.band === band));
  const best = new Map();                                  // one line per station and band: its best SNR
  for (const s of spots) { const k = s.call + '|' + s.band; if (!best.has(k) || s.snr > best.get(k).snr) best.set(k, s); }
  const heard = new Map();                                 // spots per station, for the dot size
  for (const s of spots) heard.set(s.call, (heard.get(s.call) || 0) + 1);
  const now = Date.now() / 1000, rows = [...best.values()].filter((s) => gridLL(s.grid));
  const fade = (t) => Math.max(0.3, 1 - (now - t) / (+$('hours').value * 3600));
  const far = rows.filter((s) => s.km).sort((a, b) => b.km - a.km).slice(0, 5);
  map.set({
    home: S.home, homeLabel: `Home · ${S.info ? S.info.qth : ''}`,
    lines: S.home ? rows.map((s) => ({id: s.call + '|' + s.band, from: S.home, to: gridLL(s.grid), band: s.band,
      w: Math.max(1, Math.min(4, (s.snr + 30) / 7)), op: 0.7 * fade(s.t)})) : [],
    stations: rows.map((s) => ({id: s.call + '|' + s.band, ll: gridLL(s.grid), band: s.band, n: heard.get(s.call), op: fade(s.t),
      sel: S.sel === s.call + '|' + s.band,
      title: `${s.call} ${s.grid} · ${s.band} · SNR ${s.snr} dB${s.dbm != null ? ' · ' + s.dbm + ' dBm' : ''}${s.km ? ' · ' + Math.round(s.km).toLocaleString() + ' km' : ''} · ${utc(s.t)} UTC`})),
    labels: [...far, ...rows.filter((s) => S.sel === s.call + '|' + s.band)].map((s) => ({ll: gridLL(s.grid), text: s.call})),
  });
  const calls = new Set(spots.map((s) => s.call));
  $('fSpots').textContent = spots.length; $('fSt').textContent = calls.size;
  const f = spots.filter((s) => s.km).sort((a, b) => b.km - a.km)[0];
  $('fFar').textContent = f ? `${f.call} · ${Math.round(f.km).toLocaleString()} km (${f.band})` : '–';
  $('spots').innerHTML = spots.slice(0, 200).map((s) => `<tr data-grid="${esc(s.grid || '')}" data-id="${esc(s.call + '|' + s.band)}"><td>${utc(s.t)}</td><td style="color:${bandColor(s.band)}">${s.band}</td>
    <td>${esc(s.call)}</td><td>${esc(s.grid || '')}</td><td>${s.snr}</td><td>${s.km ? Math.round(s.km).toLocaleString() : ''}</td></tr>`).join('')
    || '<tr><td colspan="6" class="shut">No spots in this period.</td></tr>';
}
$('spots').addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-grid]'); if (!tr) return;
  S.sel = tr.dataset.id; draw(); map.focus(gridLL(tr.dataset.grid));
});
$('hours').addEventListener('change', draw);
$('band').addEventListener('change', draw);

async function conditions() {
  try {
    const p = await api('/api/propagation?minutes=30');
    const rows = BANDS.map((b) => p.bands.find((x) => x.band === b) || {band: b, wspr: 0, ft8: 0, ft4: 0, cw: 0, stations: 0, far_km: 0});
    $('cond').innerHTML = rows.map((x) => { const open = x.wspr + x.ft8 + x.ft4 + (x.cw || 0) > 0;
      return `<tr><td class="${open ? 'open' : 'shut'}" style="color:${open ? bandColor(x.band) : ''}">${x.band}${open ? ' ●' : ''}</td><td>${x.wspr || ''}</td><td>${x.ft8 || ''}</td><td>${x.ft4 || ''}</td><td>${x.cw || ''}</td>
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
