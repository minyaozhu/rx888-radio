// Bay Maritime, Live: AIS ships on the Bay map, marine radio transcript and listen, NOAA weather radio.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error(r.statusText); return r.json(); };
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const hms = (t) => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
const mhz = (f) => (f / 1e6).toFixed(3);
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`; };

// Marine channel numbers (US), for labels
const MARINE_CH = {156800000: '16 (distress, calling)', 156650000: '13 (bridge to bridge)', 156700000: '14 (VTS San Francisco)',
  156600000: '12 (VTS San Francisco)', 157100000: '22A (Coast Guard)', 156450000: '09 (calling)', 156300000: '06 (safety)',
  156550000: '11', 157050000: '21A (Coast Guard)', 156400000: '08', 156500000: '10', 156975000: '19A', 157000000: '20A',
  156350000: '07A', 156900000: '18A', 156950000: '19', 156250000: '05A', 157150000: '23A', 157200000: '24', 157425000: '88A'};
const chLabel = (f) => { const k = Math.round(f / 25000) * 25000; return MARINE_CH[k] ? `ch ${MARINE_CH[k]}` : ''; };
const NOAA = {162400000: 'WX2 162.400', 162425000: 'WX4 162.425', 162450000: 'WX5 162.450', 162475000: 'WX3 162.475',
  162500000: 'WX6 162.500', 162525000: 'WX7 162.525', 162550000: 'WX1 162.550'};

const S = {session: null, status: {}, live: false, recs: [], after: -1, vessels: [], sel: null, playing: null};

// ------------------------------------------------------------------ map
const CATS = {cargo: ['#2A7DE1', 'Cargo'], tanker: ['#C0392B', 'Tanker'], passenger: ['#8E44AD', 'Passenger / ferry'],
  tug: ['#E67E22', 'Tug / towing'], 'high speed': ['#D35400', 'High speed'], fishing: ['#16A085', 'Fishing'],
  pleasure: ['#E91E63', 'Pleasure / sailing'], special: ['#2C3E50', 'Pilot, SAR, law, military'], other: ['#7F8C8D', 'Other / unknown']};
$('legend').innerHTML = Object.values(CATS).map(([c, n]) => `<span><i style="background:${c}"></i>${n}</span>`).join('')
  + '<span>· ships not heard for 15 min fade</span>';
const map = L.map('map', {zoomSnap: 0.25}).setView([37.80, -122.36], 11);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 17,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
for (const ms of [200, 800, 2000]) setTimeout(() => map.invalidateSize(), ms);
new ResizeObserver(() => map.invalidateSize()).observe($('map'));
const shipLayer = L.layerGroup().addTo(map), trackLayer = L.layerGroup().addTo(map);

function heading(v) {
  if (v.heading != null && v.heading >= 0 && v.heading < 360) return v.heading;       // 511 = not available
  if (v.course != null && v.course >= 0 && v.course < 360) return v.course;           // 360 = not available
  return null;
}
function shipIcon(v, sel) {
  const col = (CATS[v.category] || CATS.other)[0], h = heading(v), moving = (v.speed || 0) >= 0.5 && v.speed < 102;
  const shape = h == null || !moving
    ? `<circle r="5" fill="${col}" stroke="#fff" stroke-width="1.5"/>`
    : `<path d="M0,-10 L5,-2 L5,8 L-5,8 L-5,-2 Z" fill="${col}" stroke="#fff" stroke-width="1.2" transform="rotate(${Math.round(h)})"/>`;
  const ring = sel ? '<circle r="13" fill="none" stroke="#A3316F" stroke-width="2.5"/>' : '';
  const name = v.shipname || '';
  return L.divIcon({className: 'ship-ic', iconSize: [24, 24], iconAnchor: [12, 12],
    html: `<svg width="24" height="24" viewBox="-12 -12 24 24">${ring}${shape}</svg>${name && (map.getZoom() >= 12 || sel) ? `<span class="ship-lab">${esc(name)}</span>` : ''}`});
}
function vesselText(v) {
  return [v.shipname || `MMSI ${v.mmsi}`, v.shiptype_text ? v.shiptype_text.replace(/ - all ships of this type/i, '') : '',
    v.speed != null && v.speed < 102 ? `${v.speed.toFixed(1)} kn` : '', heading(v) != null ? `${Math.round(heading(v))}°` : '',
    v.destination ? `to ${v.destination}` : '', v.status_text || ''].filter(Boolean).join(' · ');
}
function drawShips() {
  shipLayer.clearLayers(); trackLayer.clearLayers();
  const now = Date.now() / 1000;
  for (const v of S.vessels) {
    if (v.lat == null) continue;
    const stale = now - (v.pos_t || v.last) > 900, sel = S.sel && S.sel.mmsi === v.mmsi;
    if (v.track && v.track.length > 1) {
      L.polyline(v.track.map((p) => [p[1], p[2]]), {color: (CATS[v.category] || CATS.other)[0], weight: sel ? 3 : 1.5,
        opacity: stale ? 0.2 : 0.55, interactive: false}).addTo(trackLayer);
    }
    L.marker([v.lat, v.lon], {icon: shipIcon(v, sel), opacity: stale ? 0.4 : 1, zIndexOffset: sel ? 1000 : 0})
      .bindTooltip(`${esc(vesselText(v))}<br>MMSI ${v.mmsi}${v.callsign ? ' · ' + esc(v.callsign) : ''} · heard ${ago(v.last)}`, {direction: 'top', offset: [0, -10]})
      .on('click', () => selectVessel(v)).addTo(shipLayer);
  }
}
map.on('zoomend', drawShips);
function drawVesselList() {
  $('fVes').textContent = S.vessels.length;
  $('vlist').innerHTML = S.vessels.slice(0, 80).map((v) => `<li data-mmsi="${v.mmsi}"><span><b>${esc(v.shipname || 'MMSI ' + v.mmsi)}</b>
      <span style="color:${(CATS[v.category] || CATS.other)[0]}">●</span> ${esc((CATS[v.category] || CATS.other)[1])}</span>
      <span>${v.speed != null && v.speed < 102 ? v.speed.toFixed(1) + ' kn · ' : ''}${ago(v.last)}</span></li>`).join('')
    || '<li><span style="color:var(--muted)">No ships decoded in this session.</span></li>';
}
$('vlist').addEventListener('click', (e) => { const li = e.target.closest('li[data-mmsi]'); if (li) selectVessel(S.vessels.find((v) => v.mmsi === +li.dataset.mmsi), true); });
function selectVessel(v, pan) {
  if (!v) return;
  S.sel = {mmsi: v.mmsi};
  $('dTime').textContent = v.pos_t ? hms(v.pos_t) : hms(v.last);
  $('dWhat').textContent = v.shipname || `MMSI ${v.mmsi}`;
  $('dRaw').textContent = vesselText(v);
  $('dMeta').textContent = `MMSI ${v.mmsi}${v.callsign ? ' · call sign ' + v.callsign : ''}${v.imo ? ' · IMO ' + v.imo : ''}`
    + (v.to_bow != null && v.to_stern != null ? ` · ${v.to_bow + v.to_stern} m long` : '') + ` · ${v.messages} AIS messages`;
  $('clip').disabled = true;
  if (pan && v.lat != null) map.setView([v.lat, v.lon], Math.max(map.getZoom(), 13));
  drawShips();
}

// ------------------------------------------------------------------ marine radio transcript
function drawLog() {
  const items = S.recs.filter((r) => r.text).slice(-300).reverse();
  $('log').innerHTML = items.map((r) => `<li data-i="${r.i}"><span class="t">${hms(r.t0)}</span>
      <span class="tx">${esc(r.text)}</span><span class="ch">${mhz(r.freq_hz)} ${esc(chLabel(r.freq_hz))} · ${(r.t1 - r.t0).toFixed(1)} s</span></li>`).join('')
    || `<li><span class="t">—</span><span class="tx ns">No marine radio calls transcribed${S.recs.length ? ` (${S.recs.length} transmissions without recognisable speech)` : ''}.</span></li>`;
}
$('log').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-i]'); if (!li) return;
  const r = S.recs.find((x) => x.i === +li.dataset.i);
  S.sel = {rec: r};
  $('dTime').textContent = hms(r.t0); $('dWhat').textContent = `${mhz(r.freq_hz)} ${chLabel(r.freq_hz)}`;
  $('dRaw').textContent = `“${r.text}”`;
  $('dMeta').textContent = `${(r.t1 - r.t0).toFixed(1)} s · carrier +${r.peak_db} dB${r.text_atc && r.text_atc !== r.text ? ' · with marine hint: “' + r.text_atc + '”' : ''}`;
  $('clip').disabled = false;
});
$('clip').addEventListener('click', () => {
  const r = S.sel && S.sel.rec; if (!r) return;
  if (S.playing) S.playing.pause();
  S.playing = new Audio(`/api/marine/clip/${S.session}/${r.clip}`); S.playing.play();
});

// ------------------------------------------------------------------ listen: marine voice (follow activity) and weather radio
const audio = {ctx: null, gain: null, ws: null, next: 0, ssrc: null, on: false, lastKey: 0, fixed: null};
const SILENT_MS = 800;
const freqOf = (ssrc) => Object.entries(S.status.channels || {}).find(([, s]) => s === ssrc)?.[0];
function wantedSsrc() {
  if (audio.fixed) return audio.fixed;                    // a weather radio station
  const ch = S.status.channels || {}, mode = $('chan').value;
  if (mode !== 'follow') return ch[mode];
  const keyed = S.status.keyed || {}, cur = freqOf(audio.ssrc);
  if (cur && keyed[cur] != null) { audio.lastKey = Date.now(); return audio.ssrc; }
  if (cur && Date.now() - audio.lastKey < SILENT_MS) return audio.ssrc;
  const on = Object.entries(keyed).filter(([f]) => ch[f]).sort((a, b) => b[1] - a[1]);
  if (on.length) { audio.lastKey = Date.now(); return ch[on[0][0]]; }
  return audio.ssrc && cur ? audio.ssrc : (ch['156800000'] || Object.values(ch)[0]);
}
function connect(ssrc) {
  if (audio.ws) { const w = audio.ws; audio.ws = null; w.onmessage = w.onclose = null; w.close(); }
  audio.ssrc = ssrc;
  if (!ssrc || !audio.on) return;
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + `/ws/audio/${ssrc}`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    const rate = new DataView(e.data).getUint32(0, true), pcm = new Int16Array(e.data, 4);
    if (!pcm.length) return;
    const buf = audio.ctx.createBuffer(1, pcm.length, rate), out = buf.getChannelData(0);
    for (let k = 0; k < pcm.length; k++) out[k] = pcm[k] / 32768;
    const src = audio.ctx.createBufferSource(); src.buffer = buf; src.connect(audio.gain);
    const now = audio.ctx.currentTime;
    if (audio.next < now + 0.05 || audio.next > now + 1.0) audio.next = now + 0.2;
    src.start(audio.next); audio.next += buf.duration;
  };
  ws.onclose = () => { if (audio.ws === ws && audio.on) setTimeout(() => audio.on && connect(wantedSsrc()), 1000); };
  audio.ws = ws;
}
function setOn(on) {
  audio.on = on;
  if (on) {
    if (!audio.ctx) { audio.ctx = new AudioContext(); audio.gain = audio.ctx.createGain(); audio.gain.connect(audio.ctx.destination); }
    audio.ctx.resume(); audio.gain.gain.value = +$('vol').value;
    connect(wantedSsrc());
  } else { connect(null); audio.fixed = null; }
  $('listen').textContent = on ? 'Stop' : 'Listen'; $('listen').classList.toggle('on', on);
  drawChans();
}
$('listen').addEventListener('click', () => { audio.fixed = null; setOn(!audio.on); });
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });
$('chan').addEventListener('change', () => { audio.fixed = null; if (audio.on) connect(wantedSsrc()); drawChans(); });
$('wx').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-ssrc]'); if (!b) return;
  if (audio.on && audio.fixed === +b.dataset.ssrc) { setOn(false); return; }
  audio.fixed = +b.dataset.ssrc;
  if (!audio.on) setOn(true); else { connect(audio.fixed); drawChans(); }
});
setInterval(async () => {
  if (!audio.on || audio.fixed || $('chan').value !== 'follow' || !S.session) return;
  try {
    S.status = (await api(`/api/marine?session=${S.session}`)).status || {};
    const want = wantedSsrc(); if (want && want !== audio.ssrc) connect(want);
    drawChans();
  } catch (e) { /* main poll reports */ }
}, 300);

function drawChans() {
  const ch = S.status.channels || {}, keyed = S.status.keyed || {};
  const counts = {};
  for (const r of S.recs) counts[Math.round(r.freq_hz)] = (counts[Math.round(r.freq_hz)] || 0) + 1;
  const freqs = Object.keys(ch).sort();
  const prev = $('chan').value || 'follow';
  $('chan').innerHTML = '<option value="follow">Follow activity</option>' + freqs.map((f) => `<option value="${f}">${mhz(+f)} ${esc(chLabel(+f))}</option>`).join('');
  $('chan').value = [...$('chan').options].some((o) => o.value === prev) ? prev : 'follow';
  const cur = audio.ssrc;
  $('now').textContent = !audio.on ? '' : audio.fixed ? 'playing weather radio' : freqOf(cur) ? `playing ${mhz(+freqOf(cur))}` : '';
  $('chans').innerHTML = freqs.map((f) => `<li data-f="${f}" class="${keyed[f] != null ? 'on' : ''} ${ch[f] === cur && audio.on && !audio.fixed ? 'sel' : ''}">
      <b>${mhz(+f)}</b> ${esc(chLabel(+f))} · ${counts[f] || 0}${keyed[f] != null ? ' · ON AIR' : ''}</li>`).join('')
    || `<li>${S.status.listening ? 'no marine voice heard yet' : 'marine window not active'}</li>`;
  const wx = S.status.weather || {};
  $('wx').innerHTML = Object.entries(wx).sort().map(([f, ssrc]) => `<button type="button" data-ssrc="${ssrc}" class="${audio.on && audio.fixed === ssrc ? 'on' : ''}">${esc(NOAA[f] || mhz(+f))}</button>`).join('')
    || '<span class="lanesub">available while the marine window is active</span>';
}
$('chans').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-f]'); if (!li) return;
  audio.fixed = null; $('chan').value = li.dataset.f;
  if (!audio.on) setOn(true); else connect(wantedSsrc());
  drawChans();
});

// ------------------------------------------------------------------ data
async function loadSession(name) {
  const info = await api('/api/marine' + (name ? `?session=${name}` : ''));
  S.session = info.session; S.recs = []; S.after = -1;
  $('session').innerHTML = info.sessions.map((s) => `<option ${s === info.session ? 'selected' : ''}>${s}</option>`).join('');
  await poll();
}
async function poll() {
  try {
    const info = await api(`/api/marine?session=${S.session}`);
    S.status = info.status || {}; S.live = info.live;
    const st = S.status, ais = st.ais || {};
    $('fLive').className = 'live' + (S.live && st.listening ? '' : ' off');
    $('fLive').lastChild.textContent = !S.live ? 'recorded' : st.listening ? 'listening' : `away on ${st.window}`;
    $('fAis').textContent = ais.running ? `receiving · ${ais.messages} messages` : ais.error ? 'decoder missing' : 'off';
    const notice = !S.live ? 'This session isn\'t running. Start voice_session.py with a marine window (see README).'
      : !st.listening ? 'The marine window runs only while the receiver is held here: press “Switch receiver here” above. (It\'s left out of the normal rotation, because this antenna hears few ships.)'
      : ais.running && !ais.messages ? 'Listening, but no AIS heard yet. Ships transmit from sea level and need a clear line of sight to the antenna; an outdoor antenna with a view of the Bay makes the difference.'
      : '';
    $('notice').textContent = notice; $('notice').style.display = notice ? '' : 'none';
    const add = await api(`/api/marine/transmissions?session=${S.session}&after=${S.after}`);
    if (add.length) { S.recs.push(...add); S.after = S.recs[S.recs.length - 1].i; }
    $('nTx').textContent = S.recs.filter((r) => r.text).length;
    drawLog();
    const ves = await api(`/api/marine/vessels?session=${S.session}`);
    S.vessels = ves.vessels || [];
    drawShips(); drawVesselList(); drawChans();
  } catch (e) {
    $('notice').textContent = `No marine session yet (${e.message}). Start voice_session.py with a marine window.`;
    $('notice').style.display = '';
  }
}
$('session').addEventListener('change', () => loadSession($('session').value));
loadSession().catch(() => poll()).finally(() => setInterval(poll, 4000));
