// SPDX-License-Identifier: GPL-3.0-or-later
// UHF survey: the 200-1000 MHz sweep (uhf_survey.json), what each band would give as a tab, and the signals measured.
// "Survey live" sweeps the chart's range again (POST /api/survey/stream, rxapp/jobs.py Scanner.survey) and redraws the
// chart window by window; the last live survey (GET /api/survey/last) is laid over the morning's on load.
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);

// Band plan (US / Bay Area), for labels
const BANDS = [
  [200, 216, 'VHF TV ch 11–13'], [216, 220, 'AMTS / data'], [220, 225, '1.25 m amateur'],
  [225, 400, 'Military UHF air, SATCOM'], [400, 406, 'Radiosondes'], [406, 406.1, '406 MHz beacons'], [406.1, 420, 'Federal land mobile'],
  [420, 433, '70 cm amateur'], [433, 435, '433.92 ISM'], [435, 450, '70 cm amateur'], [450, 470, 'UHF business / public safety'],
  [470, 512, 'UHF T-band, TV ch 14–20'], [512, 608, 'UHF TV ch 21–36'], [608, 698, '600 MHz mobile'], [698, 758, '700 MHz LTE'],
  [758, 769, 'FirstNet LTE'], [769, 776, '700 MHz public safety'], [776, 806, '700 MHz LTE up / public safety'],
  [806, 824, '800 MHz trunking (mobile)'], [824, 849, 'Cellular uplink'], [849, 851, 'Air-ground'], [851, 869, '800 MHz trunking'],
  [869, 894, 'Cellular 850'], [894, 902, 'Broadband'], [902, 928, '33 cm ISM'], [928, 929, 'Data'], [929, 932, 'Paging'],
  [932, 960, 'Fixed / 900 MHz'], [960, 1000, 'Aeronautical (DME)']];
const bandOf = (mhz) => (BANDS.find(([lo, hi]) => mhz >= lo && mhz < hi) || [0, 0, '—'])[2];

// What each band would give as a tab. verdict: go | check | legal | no.  zoom: a close-up in the data, or a range.
const OPPS = [
  {id: 'p25', title: '700 MHz public safety (P25 trunked)', freq: '769–775 MHz', verdict: 'go', label: 'Worth a tab: biggest',
   zoom: 'p25',
   measured: ['772.30 MHz on all the time at +26 dB: a trunking control channel',
     '773.18, 772.60 and 774.40 MHz on 70–85% of the time; several more come and go',
     'Pattern of a P25 system: one channel that never stops, plus busy voice channels (likely the regional EBRCS / SVRCS / BayRICS networks)'],
   shows: 'Who is talking (talkgroup, radio ID), live audio, transcripts and incidents on the map: the Public safety tab, for the regional digital systems.',
   needs: 'A P25 decoder (OP25 or dsd-fme; SDRTrunk for Phase II TDMA voice) fed with IQ from radiod, plus the talkgroup list.',
   caveats: 'Many police talkgroups are encrypted and can\'t be heard; fire, EMS and public works are often in the clear. Biggest build of these.',
   effort: 'effort: high'},
  {id: 'uhf', title: 'UHF business and public safety', freq: '450–470 MHz (and 483–490)', verdict: 'go', label: 'Worth a tab: quickest',
   zoom: 'uhf',
   measured: ['At least 8 channels active 10–23% of the time, up to +20 dB: 451.85, 452.73, 453.99, 461.89, 461.92, 462.05, 462.14 MHz',
     'T-band 483.31 and 490.21 MHz come and go (6–11%)',
     '454.34 MHz is on all the time: a control or data channel'],
   shows: 'Business, schools, transit and some public-safety voice, recorded and transcribed like the existing public-safety bands.',
   needs: 'Plain FM voice works today: a new recorder window in voice_session.py (safety profile, ~4 MHz per window). Digital channels (DMR / NXDN) would need dsd-fme later.',
   caveats: 'Part of it is digital and won\'t transcribe until a decoder is added.',
   effort: 'effort: low'},
  {id: 'sonde', title: 'Weather balloons (radiosondes)', freq: '400–406 MHz', verdict: 'check', label: 'Check at 4 pm',
   zoom: 'sonde',
   measured: ['Nothing on the air at the time of the sweep, as expected',
     'Oakland (National Weather Service) launches around 4 am and 4 pm local time (00 and 12 UTC); each balloon sends for about two hours'],
   shows: 'The balloon\'s track on a map with altitude, temperature, humidity and wind, and where it will land. A display tab, no voice.',
   needs: 'A radiosonde decoder for the Vaisala RS41 (e.g. the open-source rs41 demodulator, which takes audio or IQ). Confirm reception first: a 4:15 pm sweep of 400–406 MHz.',
   caveats: 'Only a few hours a day; reception depends on the balloon\'s distance and height (they\'re easy to hear once up).',
   effort: 'effort: medium'},
  {id: 'adsb', title: 'Aircraft positions (ADS-B)', freq: '1090 MHz', verdict: 'check', label: 'Not measured yet',
   zoom: null,
   measured: ['Outside this sweep (200–1000 MHz), but within the tuner\'s range (to 1700 MHz)',
     'Not yet tested from this antenna'],
   shows: 'Live aircraft positions, altitude and call signs on the Airband map, matched to the controllers\' transmissions.',
   needs: 'A 2+ MHz IQ channel at 1090 MHz into an ADS-B decoder (readsb or dump1090 reading IQ from stdin), like AIS-catcher today. First a sweep of 1080–1100 MHz.',
   caveats: 'Ground-level antennas lose distant aircraft; nearby SFO / OAK / SJC traffic should still come through.',
   effort: 'effort: medium'},
  {id: 'pager', title: 'Pagers', freq: '929–932 MHz', verdict: 'legal', label: 'Not allowed to decode',
   zoom: 'pager',
   measured: ['929.61 MHz on the air about a third of the time at +23 dB; 929.93 and 931.73 MHz active too',
     'Strong and busy: decodable with the multimon-ng already built here (POCSAG / FLEX)'],
   shows: 'Only what is lawful: how busy each paging channel is over the day, from signal level. No message content and no addresses.',
   needs: 'Nothing new for an activity-only view (it is a level meter). Decoding content is not something to build.',
   caveats: 'US federal law (ECPA, 18 U.S.C. 2511) makes intercepting display/text pager messages illegal, and they often carry medical and personal details (hospital and on-call paging). Don\'t decode, store or publish them.',
   effort: 'effort: low (activity only)'},
  {id: 'ism', title: 'Sensors on 433 / 915 MHz', freq: '433.92, 902–928 MHz', verdict: 'no', label: 'Too little here',
   zoom: 'ism',
   measured: ['20 seconds on 432–436 MHz: no sensor bursts', '902–928 MHz: two short bursts at 905.16–905.19 MHz in 20 s'],
   shows: 'Weather stations, tire pressure and doorbell sensors nearby (rtl_433, which can read IQ from radiod).',
   needs: 'rtl_433 reading IQ from stdin.', caveats: 'Little to show from this location; a longer listen might find a few.', effort: 'effort: low'},
  {id: 'trunk800', title: '800 MHz trunked radio', freq: '851–869 MHz', verdict: 'no', label: 'Nothing heard',
   zoom: [851, 869], measured: ['Nothing in 20 seconds per window'], shows: '—',
   needs: '—', caveats: 'The region\'s trunked activity that reaches this antenna is on 700 MHz instead.', effort: ''},
  {id: 'mil', title: 'Military air', freq: '225–400 MHz', verdict: 'no', label: 'Needs a long test',
   zoom: [225, 400], measured: ['Only steady spurs and local carriers in 3 s per window; no traffic seen'],
   shows: 'AM voice from military aircraft and bases (Travis, Moffett).', needs: 'An overnight dwell to see if anything is heard; then the airband code (AM) as is.',
   caveats: 'Traffic is occasional; 3 seconds per window would miss it.', effort: ''},
  {id: 'ham70', title: '70 cm amateur repeaters', freq: '420–450 MHz', verdict: 'no', label: 'Too little',
   zoom: [420, 450], measured: ['One short burst at 442.89 MHz'], shows: 'Repeater conversations.', needs: 'The FM recorder as is.',
   caveats: 'Busier in the evening; worth another look then.', effort: ''},
  {id: 'tv', title: 'TV and mobile phone signals', freq: '470–894 MHz', verdict: 'no', label: 'Not for listening',
   zoom: [470, 900], measured: ['Digital TV pilots on channels 12, 19, 20, 28, 29, 30 and 35; LTE carriers at 617 and 734–756 MHz'],
   shows: '—', needs: '—', caveats: 'ATSC TV can\'t be decoded with this setup; cellular is private (and illegal to intercept).', effort: ''},
];
const VERDICT = {go: 'v-go', check: 'v-check', legal: 'v-legal', no: 'v-no'};
const KIND = {signal: 'signal', spur: 'tuner spur (16 MHz harmonic)', tv: 'TV pilot', local: 'steady local carrier'};

let D = null, view = null;
const LIVE = {running: false, win: null, last: null};

// ------------------------------------------------------------------ chart
const W = 1200, H = 300, M = {l: 44, r: 12, t: 26, b: 28};
function series(lo, hi) {
  if (view && view.zoom) {
    const z = D.zooms[view.zoom];
    return {f: z.f, av: z.av, mx: z.mx, duty: z.duty, step: z.bin_hz, secs: z.secs};
  }
  const f = [], av = [], mx = [];
  D.av.forEach((v, i) => {
    const x = D.range_mhz[0] + (i + 0.5) * D.step_hz / 1e6;
    if (x >= lo && x <= hi) { f.push(x); av.push(v); mx.push(D.mx[i]); }
  });
  return {f, av, mx, duty: null, step: D.step_hz, secs: D.first_pass_secs};
}
function draw() {
  const [lo, hi] = view.range, s = series(lo, hi);
  const top = Math.max(30, Math.ceil(Math.max(...s.mx.filter((v) => v != null)) / 10) * 10);
  const X = (f) => M.l + (f - lo) / (hi - lo) * (W - M.l - M.r), Y = (v) => M.t + (1 - Math.max(0, Math.min(top, v)) / top) * (H - M.t - M.b);
  const parts = [];
  // bands worth a tab / others, as background strips
  for (const o of OPPS) {
    const r = o.zoom ? (Array.isArray(o.zoom) ? o.zoom : [D.zooms[o.zoom].lo, D.zooms[o.zoom].hi]) : null;
    if (!r || r[1] < lo || r[0] > hi) continue;
    const a = X(Math.max(lo, r[0])), b = X(Math.min(hi, r[1])), go = o.verdict === 'go' || o.verdict === 'check';
    if (!go && o.verdict !== 'legal') continue;                // only the bands a tab could use, and the one that's off-limits
    parts.push(`<rect x="${a}" y="${M.t}" width="${Math.max(1, b - a)}" height="${H - M.t - M.b}" fill="var(--${go ? 'band-go' : 'band-no'})"
      stroke="var(--${go ? 'band-go-edge' : 'magenta'})" stroke-width="1" ${go ? '' : 'stroke-dasharray="3 3"'}/>`);
    if (b - a > 34) parts.push(`<text class="bandlab" x="${(a + b) / 2}" y="${M.t - 8}" text-anchor="middle">${esc(o.title.split(' (')[0].replace('UHF business and public safety', 'UHF voice'))}</text>`);
  }
  // the tuner window a live survey is sweeping now
  if (LIVE.running && LIVE.win && LIVE.win[1] > lo && LIVE.win[0] < hi) {
    const a = X(Math.max(lo, LIVE.win[0])), b = X(Math.min(hi, LIVE.win[1]));
    parts.push(`<rect class="sweep" x="${a}" y="${M.t}" width="${Math.max(2, b - a)}" height="${H - M.t - M.b}"/>`);
  }
  // grid and axes
  for (let v = 0; v <= top; v += 10) parts.push(`<g class="grid"><line x1="${M.l}" x2="${W - M.r}" y1="${Y(v)}" y2="${Y(v)}"/></g>
    <g class="axis"><text x="${M.l - 6}" y="${Y(v) + 4}" text-anchor="end">${v}</text></g>`);
  const span = hi - lo, tick = [0.5, 1, 2, 5, 10, 25, 50, 100].find((t) => span / t <= 12);
  for (let t = Math.ceil(lo / tick) * tick; t <= hi + 1e-9; t += tick) {
    parts.push(`<g class="axis"><line x1="${X(t)}" x2="${X(t)}" y1="${H - M.b}" y2="${H - M.b + 4}"/><text x="${X(t)}" y="${H - 8}" text-anchor="middle">${+t.toFixed(1)}</text></g>`);
  }
  // peak (area) and average (line)
  const pts = (arr) => s.f.map((f, i) => arr[i] == null ? null : [X(f), Y(arr[i])]).filter(Boolean);
  const pm = pts(s.mx), pa = pts(s.av);
  if (pm.length) parts.push(`<path d="M${pm[0][0]},${Y(0)} ${pm.map((p) => 'L' + p.join(',')).join(' ')} L${pm[pm.length - 1][0]},${Y(0)} Z" fill="var(--series-fill)" stroke="none"/>`);
  if (pa.length) parts.push(`<path d="M${pa.map((p) => p.join(',')).join(' L')}" fill="none" stroke="var(--series)" stroke-width="${s.f.length > 1500 ? 1.2 : 2}" stroke-linejoin="round"/>`);
  parts.push(`<line id="xhair" x1="0" x2="0" y1="${M.t}" y2="${H - M.b}" stroke="var(--muted)" stroke-width="1" visibility="hidden"/>`);
  parts.push(`<text class="axis" x="${M.l - 6}" y="${M.t - 10}" text-anchor="end" style="font:500 11px 'IBM Plex Mono',monospace;fill:var(--muted)">dB</text>`);
  const chart = $('chart');
  chart.querySelector('svg')?.remove();
  chart.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Spectrum from ${lo} to ${hi} MHz: average and peak level above the noise floor">${parts.join('')}</svg>`);
  $('caption').textContent = `${lo}–${hi} MHz · ${s.step >= 1e5 ? 'strongest in each 100 kHz' : `${(s.step / 1e3).toFixed(1)} kHz bins`} · ${s.secs} s per window`
    + (view.zoom && D.zooms[view.zoom].secs === 20 ? ' (the 20-second look)' : '');
  // hover: crosshair + tooltip on the nearest point
  const svg = chart.querySelector('svg'), tip = $('tip'), xh = svg.querySelector('#xhair');
  svg.onpointermove = (e) => {
    const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * W;
    if (x < M.l || x > W - M.r) { svg.onpointerleave(); return; }
    const f = lo + (x - M.l) / (W - M.l - M.r) * (hi - lo);
    let i = 0, best = Infinity;
    s.f.forEach((g, k) => { const d = Math.abs(g - f); if (d < best) { best = d; i = k; } });
    xh.setAttribute('x1', X(s.f[i])); xh.setAttribute('x2', X(s.f[i])); xh.setAttribute('visibility', 'visible');
    tip.innerHTML = `<b>${s.f[i].toFixed(s.step >= 1e5 ? 1 : 3)} MHz</b> · ${esc(bandOf(s.f[i]))}<br>average ${s.av[i] ?? '—'} dB · peak ${s.mx[i] ?? '—'} dB`
      + (s.duty ? `<br>on the air ${Math.round(100 * s.duty[i])}%` : '');
    tip.style.display = 'block';
    const cx = e.clientX - chart.getBoundingClientRect().left, cw = chart.clientWidth;
    tip.style.left = Math.min(cx + 12, cw - tip.offsetWidth - 4) + 'px';
    tip.style.top = Math.max(0, (e.clientY - chart.getBoundingClientRect().top) - 60) + 'px';
  };
  svg.onpointerleave = () => { tip.style.display = 'none'; xh.setAttribute('visibility', 'hidden'); };
}
function setView(v) {
  view = v;
  for (const b of $('zooms').querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.key === v.key));
  draw();
}
function viewFor(o) {
  if (!o.zoom) return null;
  if (Array.isArray(o.zoom)) return {key: o.id, range: o.zoom};
  const z = D.zooms[o.zoom];
  return {key: o.id, range: [z.lo, z.hi], zoom: o.zoom};
}

// ------------------------------------------------------------------ live survey
// lay a survey's 100 kHz cells (from lo_mhz) over the chart's data
function merge(loMhz, start, av, mx) {
  const base = Math.round((loMhz - D.range_mhz[0]) / (D.step_hz / 1e6)) + start;
  av.forEach((v, j) => { const i = base + j; if (v != null && i >= 0 && i < D.av.length) { D.av[i] = v; D.mx[i] = mx[j]; } });
}
let redrawTimer = null;
const redrawSoon = () => { if (!redrawTimer) redrawTimer = setTimeout(() => { redrawTimer = null; draw(); }, 250); };
function liveFacts() {
  const l = LIVE.last;
  $('liveFact').innerHTML = l ? `<b>live</b>${new Date(l.made * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})} · ${l.range_mhz[0]}–${l.range_mhz[1]} MHz${l.stopped ? ' (stopped early)' : ''}` : '';
}
async function startSurvey() {
  // the range on the chart (a close-up from the morning's data becomes the same range at 100 kHz)
  let [lo, hi] = view.range;
  lo = Math.max(D.range_mhz[0], Math.floor(lo * 10) / 10); hi = Math.min(D.range_mhz[1], Math.ceil(hi * 10) / 10);
  if (view.zoom) setView({key: view.key + '-live', range: [lo, hi]});
  const secs = +$('secs').value;
  LIVE.running = true; LIVE.win = null;
  $('survey').textContent = 'Stop'; $('survey').classList.add('on');
  $('surveyNote').textContent = 'taking the receiver…';
  try {
    if (window.receiverFocus) await receiverFocus.ensure((f) => f === 'pause');   // the recorder must not move the tuner
    const h = await (await fetch('/api/health')).json();
    if (h.band !== 'vhf') {
      await fetch('/api/band', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({band: 'vhf'})});
      await new Promise((r) => setTimeout(r, 1500));
    }
    $('surveyNote').textContent = `sweeping ${lo}–${hi} MHz…`;
    const t0 = Date.now();
    const r = await postStream(`/api/survey/stream?lo_mhz=${lo}&hi_mhz=${hi}&secs=${secs}`, {}, (p) => {
      merge(lo, p.start, p.av, p.mx);
      LIVE.win = [lo + p.start * D.step_hz / 1e6, lo + (p.start + p.av.length) * D.step_hz / 1e6];
      const left = (Date.now() - t0) / p.window * (p.windows - p.window) / 1000;
      $('surveyNote').textContent = `window ${p.window} of ${p.windows} · ${LIVE.win[0].toFixed(1)}–${LIVE.win[1].toFixed(1)} MHz · about ${Math.ceil(left / 60)} min left`;
      redrawSoon();
    });
    LIVE.last = {...r, range_mhz: r.range_mhz};
    $('surveyNote').textContent = `${r.stopped ? 'stopped' : 'done'}: ${r.windows} windows in ${Math.round(r.took_sec)} s · the tuner is back where it was`;
  } catch (e) {
    $('surveyNote').textContent = 'survey failed: ' + e.message;
  }
  LIVE.running = false; LIVE.win = null;
  $('survey').textContent = 'Survey live'; $('survey').classList.remove('on'); $('survey').disabled = false;
  liveFacts(); draw();
}
$('survey').addEventListener('click', async () => {
  if (!LIVE.running) { startSurvey(); return; }
  $('survey').disabled = true; $('surveyNote').textContent = 'stopping after this window…';
  try { await fetch('/api/survey/stop', {method: 'POST'}); } catch (e) { /* rxapp restarting */ }
});

// ------------------------------------------------------------------ cards
function drawCards() {
  $('cards').innerHTML = OPPS.map((o) => `<article class="panel card" id="opp-${o.id}">
      <div class="top"><h3>${esc(o.title)}</h3><span class="verdict ${VERDICT[o.verdict]}">${esc(o.label)}</span></div>
      <div class="freq">${esc(o.freq)}${o.effort ? ' · ' + esc(o.effort) : ''}</div>
      <dl>
        <dt>Measured</dt><dd><ul>${o.measured.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></dd>
        ${o.shows !== '—' ? `<dt>As a tab</dt><dd>${esc(o.shows)}</dd>` : ''}
        ${o.needs !== '—' ? `<dt>Needs</dt><dd>${esc(o.needs)}</dd>` : ''}
        <dt>Catch</dt><dd>${esc(o.caveats)}</dd>
      </dl>
      ${o.zoom ? `<button type="button" class="zoom" data-opp="${o.id}">Show on the chart</button>` : ''}
    </article>`).join('');
  $('cards').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-opp]'); if (!b) return;
    setView(viewFor(OPPS.find((o) => o.id === b.dataset.opp)));
    $('chartTitle').scrollIntoView({behavior: 'smooth', block: 'start'});
  });
}

// ------------------------------------------------------------------ signals table
function drawTable() {
  const band = $('fBand').value, all = $('fAll').checked;
  const rows = D.signals.filter((s) => (all || s.kind === 'signal') && (!band || bandOf(s.peak_mhz) === band));
  $('nSig').textContent = `${rows.length} of ${D.signals.length}`;
  $('rows').innerHTML = rows.map((s) => `<tr class="k-${s.kind}"><td>${s.peak_mhz.toFixed(4)}</td><td>${esc(bandOf(s.peak_mhz))}</td>
      <td class="num">${s.width_khz}</td><td class="num">+${s.max_db}</td><td class="num">${s.avg_db > 0 ? '+' : ''}${s.avg_db}</td>
      <td class="num">${Math.round(100 * s.duty)}%</td><td>${s.pass}</td><td>${esc(KIND[s.kind])}</td></tr>`).join('')
    || '<tr><td colspan="8" style="color:var(--muted)">No signals in this band.</td></tr>';
}

// ------------------------------------------------------------------ load
(async () => {
  try {
    D = await (await fetch('uhf_survey.json')).json();
  } catch (e) {
    $('cards').innerHTML = `<p class="notice">Couldn't load the survey data (${esc(e.message)}).</p>`;
    return;
  }
  const real = D.signals.filter((s) => s.kind === 'signal').length;
  $('facts').innerHTML = `<span><b>swept</b>${new Date(D.made * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})}</span>
    <span><b>range</b>200–1000 MHz</span><span><b>look</b>${D.first_pass_secs} s per window, 20 s on 5 bands</span>
    <span><b>signals</b>${real} (+${D.signals.length - real} spurs, pilots, local)</span><span id="liveFact"></span>`;
  try {                                                       // the last live survey, over the morning's
    const l = await (await fetch('/api/survey/last')).json();
    if (l && l.av) { merge(l.range_mhz[0], 0, l.av, l.mx); LIVE.last = l; liveFacts(); }
  } catch (e) { /* none yet */ }
  const zb = [{key: 'all', label: '200–1000 MHz', range: D.range_mhz}, ...OPPS.filter((o) => o.zoom && o.verdict !== 'no')
    .map((o) => ({...viewFor(o), label: o.freq.split(' ')[0] + ' ' + o.title.split(' (')[0].split(' ').slice(0, 2).join(' ')}))];
  $('zooms').innerHTML = zb.map((z) => `<button type="button" data-key="${z.key}" aria-pressed="false">${esc(z.label)}</button>`).join('');
  $('zooms').addEventListener('click', (e) => { const b = e.target.closest('button[data-key]'); if (b) setView(zb.find((z) => z.key === b.dataset.key)); });
  const names = [...new Set(D.signals.map((s) => bandOf(s.peak_mhz)))];
  $('fBand').innerHTML = '<option value="">All bands</option>' + names.map((n) => `<option>${esc(n)}</option>`).join('');
  $('fBand').addEventListener('change', drawTable); $('fAll').addEventListener('change', drawTable);
  drawCards(); drawTable(); setView(zb[0]);
})();

// the spectrum tile (minispec.js): while a live survey runs, its whole range with the window being swept highlighted
window.miniSpecRange = () => (LIVE.running && miniSpec.scan() ? miniSpec.scanRange('UHF survey') : null);
