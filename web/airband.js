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
const STAGE = [[420, 450], [520, 425], [640, 405], [660, 330], [575, 283], [440, 250]];
const OFFSETS = [[0, 0], [14, -14], [-14, 12], [16, 14], [-16, -12], [0, 18], [18, 0], [-18, 0], [0, -18], [20, 20], [-20, -20]];
const planes = $('planes');
const pl = {};
function drawMap() {
  const t = S.cursor;
  const occupied = {};
  const list = $('flightList');
  list.innerHTML = '';
  const seen = new Set();
  for (const f of S.flights) {
    if (f.id === '?') continue;
    const past = S.recs.filter((r) => r.f === f.id && r.t0 <= t);
    let p = pl[f.id];
    if (!p) {
      const g = el('g', {opacity: 0}, planes);
      g.style.transition = 'transform .6s ease, opacity .4s';
      el('circle', {r: 7, fill: 'var(--panel)', stroke: 'var(--ink)', 'stroke-width': 2}, g);
      el('path', {d: 'M-4 2 L0 -5 L4 2 Z', fill: 'var(--ink)'}, g);
      el('text', {x: 10, y: -8, class: 'tag'}, g).textContent = f.name.replace('…', '');
      g.addEventListener('click', () => { const r = S.recs.filter((x) => x.f === f.id && x.t0 <= S.cursor).pop(); if (r) select(r); });
      g.style.cursor = 'pointer';
      p = pl[f.id] = {g};
    }
    seen.add(f.id);
    if (!past.length || t - past[past.length - 1].t0 > 20 * 60) { p.g.setAttribute('opacity', 0); continue; }  // not heard (lately)
    const last = [...past].reverse().find((r) => PH[r.ph].stage !== null);
    const stage = last ? PH[last.ph].stage : 0, at = last ? last.t0 : past[0].t0;
    const gone = stage === 5 && t - at > 90;                         // with the tower by now
    const key = gone ? 'tower' : stage;
    const n = (occupied[key] = (occupied[key] || 0) + 1) - 1;
    const [bx, by] = gone ? [238, 218] : STAGE[stage];
    const [ox, oy] = gone ? [n * 6, n * 6] : OFFSETS[n % OFFSETS.length];
    p.g.setAttribute('opacity', gone ? 0.3 : 1);
    p.g.style.transform = `translate(${bx + ox}px, ${by + oy}px)`;
    p.g.querySelector('circle').setAttribute('stroke', S.sel && S.sel.f === f.id ? 'var(--magenta)' : 'var(--ink)');
    const li = document.createElement('li');
    li.innerHTML = `<span><b>${f.name}</b></span><span>${gone ? 'With SFO Tower' : (last ? PH[last.ph].label : 'Heard')} · ${hms(past[past.length - 1].t0)}</span>`;
    li.style.cursor = 'pointer';
    li.addEventListener('click', () => select(past[past.length - 1]));
    list.appendChild(li);
  }
  for (const id in pl) if (!seen.has(id)) { pl[id].g.remove(); delete pl[id]; }
  if (!list.children.length) list.innerHTML = '<li><span>No flights identified yet</span><span></span></li>';
}

// ------------------------------------------------------------------ detail, log
function select(r, moveTime) {
  S.sel = r;
  if (moveTime && !(S.follow && S.live)) S.cursor = r.t0 + 0.5;
  $('dTime').textContent = hms(r.t0);
  $('dFlight').textContent = r.cs ? r.cs.name : 'Unattributed';
  $('dPhase').textContent = PH[r.ph].label; $('dPhase').style.color = `var(${PH[r.ph].c})`;
  $('dRaw').textContent = r.text ? `“${r.text}”` : '(no speech recognised)';
  $('dAtc').textContent = r.text_atc && r.text_atc !== r.text ? `with ATC hint: “${r.text_atc}”` : '';
  $('dMeta').textContent = `${mhz(r.freq_hz)} MHz · ${(r.t1 - r.t0).toFixed(1)} s · carrier +${r.peak_db} dB over the floor`;
  $('clip').disabled = false;
  document.querySelectorAll('#log li').forEach((li) => li.classList.toggle('sel', +li.dataset.i === r.i));
  render();
}
function drawLog() {
  const items = S.recs.slice(-300).reverse();
  $('log').innerHTML = items.map((r) => `<li data-i="${r.i}" class="${S.sel === r ? 'sel' : ''}"><span class="t">${hms(r.t0)}</span>
    <span class="f">${mhz(r.freq_hz)}</span><span>${r.text ? esc(r.text) : '<span class="t">(no speech)</span>'}</span></li>`).join('')
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
  drawTimeline(); drawMap();
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
function wantedSsrc() {
  const ch = S.status.channels || {};
  const mode = $('chan').value;
  if (mode === 'loudest') { const f = loudest(); return f ? ch[f] : Object.values(ch)[0]; }
  if (mode === 'follow') {
    const keyed = Object.entries(S.status.keyed || {}).sort((a, b) => b[1] - a[1]);
    if (keyed.length && ch[keyed[0][0]]) { audio.lastKey = Date.now(); return ch[keyed[0][0]]; }
    return audio.ssrc || (loudest() ? ch[loudest()] : Object.values(ch)[0]);     // stay until someone else talks
  }
  return ch[mode];
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
  const prev = $('chan').value || 'loudest';
  const lf = loudest();
  $('chan').innerHTML = `<option value="loudest">Loudest${lf ? ' · ' + mhz(lf) : ''}</option><option value="follow">Follow who's talking</option>`
    + freqs.map((f) => `<option value="${f}">${mhz(+f)} MHz</option>`).join('');
  $('chan').value = [...$('chan').options].some((o) => o.value === prev) ? prev : 'loudest';
  $('chans').innerHTML = freqs.map((f) => `<li data-f="${f}" class="${keyed[f] != null ? 'on' : ''} ${ch[f] === audio.ssrc && audio.on ? 'sel' : ''}"
      title="${keyed[f] != null ? 'on the air now' : 'quiet'}"><b>${mhz(+f)}</b> · ${counts[f] || 0}${keyed[f] != null ? ' · ON AIR' : ''}</li>`).join('')
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
  for (const id in pl) { pl[id].g.remove(); delete pl[id]; }
  $('session').innerHTML = info.sessions.map((s) => `<option ${s === info.session ? 'selected' : ''}>${s}</option>`).join('');
  await poll(true);
}
async function poll(full) {
  try {
    const info = await api(`/api/airband?session=${S.session}`);
    S.status = info.status || {}; S.live = info.live;
    $('fLive').className = 'live' + (S.live ? '' : ' off');
    $('fLive').lastChild.textContent = S.live ? 'live' : 'recorded';
    if (S.status.lo) $('fWin').textContent = `${mhz(S.status.lo)}–${mhz(S.status.hi)} MHz`;
    const add = await api(`/api/airband/transmissions?session=${S.session}&after=${S.after}`);
    if (add.length || full) {
      S.recs.push(...add);
      S.after = S.recs.length ? S.recs[S.recs.length - 1].i : -1;
      derive(); drawLog();
      if (S.follow && add.length) select(add[add.length - 1]);
      if (S.cursor == null) S.cursor = S.live ? Date.now() / 1000 : (S.recs.length ? S.recs[S.recs.length - 1].t1 : Date.now() / 1000);
    }
    if (audio.on && $('chan').value === 'follow') {
      const want = wantedSsrc();
      if (want && want !== audio.ssrc) connect(want);
    }
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
