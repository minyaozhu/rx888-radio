// SPDX-License-Identifier: GPL-3.0-or-later
// L-band survey: 1000-1700 MHz, the top of the VHF/UHF tuner. Unlike the UHF survey there is no stored morning sweep:
// the chart is the last live survey of this page (GET /api/survey/last?page=lband), and "Survey live" sweeps the
// chart's range again (POST /api/survey/stream?page=lband, rxapp/jobs.py Scanner.survey), redrawing window by window.
// The cards' "Measured" lines and the signals table are worked out from that survey.
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);

const LO = 1000, HI = 1700, STEP = 0.1;                  // MHz; the survey's 100 kHz cells
const N = Math.round((HI - LO) / STEP);
const SIG_DB = 8;                                        // a cell whose peak stands this far above the floor: a signal

// Band plan (US), for labels
const BANDS = [
  [960, 1029, 'Aeronautical (DME / TACAN)'], [1029, 1031, 'Transponder interrogation (1030)'],
  [1031, 1089, 'Aeronautical (DME / TACAN)'], [1089, 1091, 'ADS-B / transponder replies (1090)'],
  [1091, 1215, 'Aeronautical (DME / TACAN)'], [1215, 1240, 'GNSS L2 (GPS 1227.6) / radar'], [1240, 1300, '23 cm amateur'],
  [1300, 1390, 'Long-range radar'], [1390, 1400, 'Fixed / medical telemetry'], [1400, 1427, 'Radio astronomy (hydrogen 1420.4)'],
  [1427, 1435, 'Medical telemetry / fixed'], [1435, 1525, 'Aeronautical telemetry (flight test)'],
  [1525, 1559, 'Satellite downlink (Inmarsat)'], [1559, 1610, 'GNSS (GPS L1 1575.42, GLONASS)'],
  [1610, 1626.5, 'Satellite phones (Iridium, Globalstar up)'], [1626.5, 1660.5, 'Satellite uplink (Inmarsat)'],
  [1660.5, 1670, 'Radio astronomy / radiosondes'], [1670, 1700, 'Weather satellites (GOES, NOAA)']];
const bandOf = (mhz) => (BANDS.find(([lo, hi]) => mhz >= lo && mhz < hi) || [0, 0, '—'])[2];

// What each band could give. verdict: go | check | legal | no.  range: the part of the chart it covers (MHz).
const OPPS = [
  {id: 'adsb', title: 'Aircraft positions (ADS-B)', freq: '1090 MHz', verdict: 'check', label: 'Most likely here',
   range: [1089, 1091], zoom: [1080, 1100],
   shows: 'Live aircraft positions, altitude and call signs on the Airband map, matched to the controllers\' transmissions.',
   needs: 'A 2+ MHz IQ channel at 1090 MHz into an ADS-B decoder (readsb or dump1090 reading IQ), like AIS-catcher today.',
   caveats: 'Replies are 120-microsecond bursts: a short look can show little even with planes overhead. SFO / OAK / SJC traffic is close enough to come through.',
   effort: 'effort: medium'},
  {id: 'ham23', title: '23 cm amateur', freq: '1240–1300 MHz', verdict: 'check', label: 'Probably quiet',
   range: [1240, 1300], zoom: [1240, 1300],
   shows: 'FM repeater conversations (most near 1282–1294 MHz), recorded and transcribed like the other voice bands.',
   needs: 'The FM recorder as is: a new window in voice_session.py.', caveats: 'Few stations use 23 cm; a short sweep will likely miss them.',
   effort: 'effort: low'},
  {id: 'inmarsat', title: 'Inmarsat safety broadcasts', freq: '1525–1559 MHz', verdict: 'check', label: 'Needs a pointed antenna',
   range: [1525, 1559], zoom: [1525, 1559],
   shows: 'SafetyNET maritime safety broadcasts (weather, navigation warnings, distress relays) from the Inmarsat-C service.',
   needs: 'An L-band patch antenna aimed at the satellite over the Americas, plus an STD-C decoder.',
   caveats: 'The satellite signal is weak: a sky-facing patch antenna is usually needed; this antenna is unlikely to hear it.',
   effort: 'effort: medium'},
  {id: 'iridium', title: 'Iridium satellite phones', freq: '1616–1626.5 MHz', verdict: 'legal', label: 'Activity only',
   range: [1616, 1626.5], zoom: [1610, 1630],
   shows: 'Only how busy the band is: the bursts of satellites passing overhead, from signal level.',
   needs: 'Nothing new for an activity view.',
   caveats: 'Its traffic is private calls, messages and pages: decoding the content is interception (ECPA, 18 U.S.C. 2511). Don\'t decode, store or publish it.',
   effort: 'effort: low (activity only)'},
  {id: 'gnss', title: 'GPS and other GNSS', freq: '1559–1610 MHz', verdict: 'no', label: 'Below the noise',
   range: [1559, 1610], zoom: [1559, 1610],
   shows: '—', needs: '—', caveats: 'Navigation signals arrive below the noise floor by design: a spectrum sweep can\'t show them.', effort: ''},
  {id: 'hline', title: 'Hydrogen line (radio astronomy)', freq: '1420.4 MHz', verdict: 'no', label: 'Needs a dish',
   range: [1400, 1427], zoom: [1400, 1427],
   shows: 'The Milky Way\'s neutral hydrogen.', needs: 'A dish or horn antenna and a low-noise amplifier, and long averaging.',
   caveats: 'Far too faint for this setup.', effort: ''},
  {id: 'goes', title: 'Weather satellite images (GOES)', freq: '1686.6–1694.1 MHz', verdict: 'no', label: 'Needs a dish',
   range: [1670, 1700], zoom: [1670, 1700],
   shows: 'Full-disk images of the Americas every 15 minutes (GOES-West HRIT).', needs: 'A ~1 m dish aimed at the satellite, a filtered LNA, and an HRIT decoder.',
   caveats: 'Not receivable with this antenna.', effort: ''},
];
const VERDICT = {go: 'v-go', check: 'v-check', legal: 'v-legal', no: 'v-no'};

const D = {av: new Array(N).fill(null), mx: new Array(N).fill(null)};
let view = null;
const LIVE = {running: false, win: null, last: null};
const fMhz = (i) => LO + (i + 0.5) * STEP;
const hasData = () => D.mx.some((v) => v != null);

// ------------------------------------------------------------------ chart
const W = 1200, H = 300, M = {l: 44, r: 12, t: 26, b: 28};
function draw() {
  const [lo, hi] = view.range, f = [], av = [], mx = [];
  for (let i = 0; i < N; i++) { const x = fMhz(i); if (x >= lo && x <= hi) { f.push(x); av.push(D.av[i]); mx.push(D.mx[i]); } }
  const top = Math.max(30, Math.ceil(Math.max(0, ...mx.filter((v) => v != null)) / 10) * 10);
  const X = (v) => M.l + (v - lo) / (hi - lo) * (W - M.l - M.r), Y = (v) => M.t + (1 - Math.max(0, Math.min(top, v)) / top) * (H - M.t - M.b);
  const parts = [];
  for (const o of OPPS) {
    const r = o.range;
    if (r[1] < lo || r[0] > hi || o.verdict === 'no') continue;
    const a = X(Math.max(lo, r[0])), b = X(Math.min(hi, r[1])), go = o.verdict !== 'legal';
    parts.push(`<rect x="${a}" y="${M.t}" width="${Math.max(2, b - a)}" height="${H - M.t - M.b}" fill="var(--${go ? 'band-go' : 'band-no'})"
      stroke="var(--${go ? 'band-go-edge' : 'magenta'})" stroke-width="1" ${go ? '' : 'stroke-dasharray="3 3"'}/>`);
    if (b - a > 34 || (hi - lo) < 50) parts.push(`<text class="bandlab" x="${(a + b) / 2}" y="${M.t - 8}" text-anchor="middle">${esc(o.title.split(' (')[0])}</text>`);
  }
  if (LIVE.running && LIVE.win && LIVE.win[1] > lo && LIVE.win[0] < hi) {
    const a = X(Math.max(lo, LIVE.win[0])), b = X(Math.min(hi, LIVE.win[1]));
    parts.push(`<rect class="sweep" x="${a}" y="${M.t}" width="${Math.max(2, b - a)}" height="${H - M.t - M.b}"/>`);
  }
  for (let v = 0; v <= top; v += 10) parts.push(`<g class="grid"><line x1="${M.l}" x2="${W - M.r}" y1="${Y(v)}" y2="${Y(v)}"/></g>
    <g class="axis"><text x="${M.l - 6}" y="${Y(v) + 4}" text-anchor="end">${v}</text></g>`);
  const span = hi - lo, tick = [0.5, 1, 2, 5, 10, 25, 50, 100].find((t) => span / t <= 12);
  for (let t = Math.ceil(lo / tick) * tick; t <= hi + 1e-9; t += tick) {
    parts.push(`<g class="axis"><line x1="${X(t)}" x2="${X(t)}" y1="${H - M.b}" y2="${H - M.b + 4}"/><text x="${X(t)}" y="${H - 8}" text-anchor="middle">${+t.toFixed(1)}</text></g>`);
  }
  // peak (area) and average (line), broken where a cell has no data yet
  const runs = (arr) => {
    const out = []; let cur = [];
    f.forEach((x, i) => { if (arr[i] == null) { if (cur.length) out.push(cur); cur = []; } else cur.push([X(x), Y(arr[i])]); });
    if (cur.length) out.push(cur);
    return out;
  };
  for (const r of runs(mx)) parts.push(`<path d="M${r[0][0]},${Y(0)} ${r.map((p) => 'L' + p.join(',')).join(' ')} L${r[r.length - 1][0]},${Y(0)} Z" fill="var(--series-fill)" stroke="none"/>`);
  for (const r of runs(av)) parts.push(`<path d="M${r.map((p) => p.join(',')).join(' L')}" fill="none" stroke="var(--series)" stroke-width="${f.length > 1500 ? 1.2 : 2}" stroke-linejoin="round"/>`);
  if (!mx.some((v) => v != null) && !LIVE.running) parts.push(`<text class="empty" x="${W / 2}" y="${H / 2}" text-anchor="middle">Not surveyed yet: press “Survey live”</text>`);
  parts.push(`<line id="xhair" x1="0" x2="0" y1="${M.t}" y2="${H - M.b}" stroke="var(--muted)" stroke-width="1" visibility="hidden"/>`);
  parts.push(`<text class="axis" x="${M.l - 6}" y="${M.t - 10}" text-anchor="end" style="font:500 11px 'IBM Plex Mono',monospace;fill:var(--muted)">dB</text>`);
  const chart = $('chart');
  chart.querySelector('svg')?.remove();
  chart.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Spectrum from ${lo} to ${hi} MHz: average and peak level above the noise floor">${parts.join('')}</svg>`);
  $('caption').textContent = `${lo}–${hi} MHz · strongest in each 100 kHz` + (LIVE.last ? ` · ${LIVE.last.secs_per_window} s per window` : '');
  const svg = chart.querySelector('svg'), tip = $('tip'), xh = svg.querySelector('#xhair');
  svg.onpointermove = (e) => {
    const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * W;
    if (x < M.l || x > W - M.r || !f.length) { svg.onpointerleave(); return; }
    const g = lo + (x - M.l) / (W - M.l - M.r) * (hi - lo);
    let i = 0, best = Infinity;
    f.forEach((v, k) => { const d = Math.abs(v - g); if (d < best) { best = d; i = k; } });
    xh.setAttribute('x1', X(f[i])); xh.setAttribute('x2', X(f[i])); xh.setAttribute('visibility', 'visible');
    tip.innerHTML = `<b>${f[i].toFixed(1)} MHz</b> · ${esc(bandOf(f[i]))}<br>` + (mx[i] == null ? 'not surveyed' : `average ${av[i]} dB · peak ${mx[i]} dB`);
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

// ------------------------------------------------------------------ what the survey found
function signals() {
  const out = [];
  for (let i = 0; i < N; i++) {
    if (D.mx[i] == null || D.mx[i] < SIG_DB) continue;
    let j = i, best = i;
    while (j + 1 < N && D.mx[j + 1] != null && D.mx[j + 1] >= SIG_DB) { j++; if (D.mx[j] > D.mx[best]) best = j; }
    const peak = fMhz(best), near16 = Math.abs(peak - Math.round(peak / 16) * 16) <= 0.1;
    out.push({mhz: peak, width: (j - i + 1) * 100, max: D.mx[best], avg: Math.max(...D.av.slice(i, j + 1).filter((v) => v != null)),
              kind: near16 ? 'spur' : 'signal'});
    i = j;
  }
  return out;
}
function measured(o) {
  const a = Math.max(0, Math.floor((o.range[0] - LO) / STEP)), b = Math.min(N, Math.ceil((o.range[1] - LO) / STEP));
  const cells = D.mx.slice(a, b), seen = cells.filter((v) => v != null);
  if (!seen.length) return 'Not surveyed yet';
  const k = cells.indexOf(Math.max(...seen)), top = cells[k];
  const n = signals().filter((s) => s.kind === 'signal' && s.mhz >= o.range[0] && s.mhz <= o.range[1]).length;
  return top < SIG_DB ? `Nothing above the noise (strongest +${top} dB)`
    : `${n} signal${n === 1 ? '' : 's'}; strongest +${top} dB at ${fMhz(a + k).toFixed(1)} MHz`;
}
function drawCards() {
  $('cards').innerHTML = OPPS.map((o) => `<article class="panel card" id="opp-${o.id}">
      <div class="top"><h3>${esc(o.title)}</h3><span class="verdict ${VERDICT[o.verdict]}">${esc(o.label)}</span></div>
      <div class="freq">${esc(o.freq)}${o.effort ? ' · ' + esc(o.effort) : ''}</div>
      <dl>
        <dt>Last survey</dt><dd>${esc(measured(o))}</dd>
        ${o.shows !== '—' ? `<dt>Could give</dt><dd>${esc(o.shows)}</dd>` : ''}
        ${o.needs !== '—' ? `<dt>Needs</dt><dd>${esc(o.needs)}</dd>` : ''}
        <dt>Catch</dt><dd>${esc(o.caveats)}</dd>
      </dl>
      <button type="button" class="zoom" data-opp="${o.id}">Show on the chart</button>
    </article>`).join('');
}
$('cards').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-opp]'); if (!b) return;
  const o = OPPS.find((x) => x.id === b.dataset.opp);
  setView({key: o.id, range: o.zoom});
  $('chartTitle').scrollIntoView({behavior: 'smooth', block: 'start'});
});
function drawTable() {
  const all = signals(), band = $('fBand').value, spurs = $('fAll').checked;
  const names = [...new Set(all.map((s) => bandOf(s.mhz)))], was = $('fBand').value;
  $('fBand').innerHTML = '<option value="">All bands</option>' + names.map((n) => `<option${n === was ? ' selected' : ''}>${esc(n)}</option>`).join('');
  const rows = all.filter((s) => (spurs || s.kind === 'signal') && (!band || bandOf(s.mhz) === band));
  $('nSig').textContent = `${rows.length} of ${all.length}`;
  $('rows').innerHTML = rows.map((s) => `<tr class="k-${s.kind}"><td>${s.mhz.toFixed(1)}</td><td>${esc(bandOf(s.mhz))}</td>
      <td class="num">${s.width}</td><td class="num">+${s.max}</td><td class="num">${s.avg > 0 ? '+' : ''}${s.avg}</td>
      <td>${s.kind === 'spur' ? 'likely tuner spur (16 MHz harmonic)' : 'signal'}</td></tr>`).join('')
    || `<tr><td colspan="6" style="color:var(--muted)">${hasData() ? 'No signals above the noise.' : 'Not surveyed yet.'}</td></tr>`;
}
$('fBand').addEventListener('change', drawTable); $('fAll').addEventListener('change', drawTable);
function facts() {
  const l = LIVE.last;
  $('facts').innerHTML = `<span><b>range</b>${LO}–${HI} MHz</span>` + (l
    ? `<span><b>last survey</b>${new Date(l.made * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})} · ${l.range_mhz[0]}–${l.range_mhz[1]} MHz${l.stopped ? ' (stopped early)' : ''}</span>
       <span><b>signals</b>${signals().filter((s) => s.kind === 'signal').length}</span>`
    : '<span><b>last survey</b>none yet</span>');
}
const refresh = () => { drawCards(); drawTable(); facts(); };

// ------------------------------------------------------------------ live survey
function merge(loMhz, start, av, mx) {
  const base = Math.round((loMhz - LO) / STEP) + start;
  av.forEach((v, j) => { const i = base + j; if (v != null && i >= 0 && i < N) { D.av[i] = v; D.mx[i] = mx[j]; } });
}
let redrawTimer = null;
const redrawSoon = () => { if (!redrawTimer) redrawTimer = setTimeout(() => { redrawTimer = null; draw(); }, 250); };
async function startSurvey() {
  let [lo, hi] = view.range;
  lo = Math.max(LO, Math.floor(lo * 10) / 10); hi = Math.min(HI, Math.ceil(hi * 10) / 10);
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
    // a fresh sweep replaces what the chart showed for its range
    for (let i = Math.round((lo - LO) / STEP); i < Math.round((hi - LO) / STEP); i++) { D.av[i] = null; D.mx[i] = null; }
    $('surveyNote').textContent = `sweeping ${lo}–${hi} MHz…`;
    const t0 = Date.now();
    const r = await postStream(`/api/survey/stream?page=lband&lo_mhz=${lo}&hi_mhz=${hi}&secs=${secs}`, {}, (p) => {
      merge(lo, p.start, p.av, p.mx);
      LIVE.win = [lo + p.start * STEP, lo + (p.start + p.av.length) * STEP];
      const left = (Date.now() - t0) / p.window * (p.windows - p.window) / 1000;
      $('surveyNote').textContent = `window ${p.window} of ${p.windows} · ${LIVE.win[0].toFixed(1)}–${LIVE.win[1].toFixed(1)} MHz · about ${Math.ceil(left / 60)} min left`;
      redrawSoon();
    });
    LIVE.last = r;
    $('surveyNote').textContent = `${r.stopped ? 'stopped' : 'done'}: ${r.windows} windows in ${Math.round(r.took_sec)} s · the tuner is back where it was`;
  } catch (e) {
    $('surveyNote').textContent = 'survey failed: ' + e.message;
  }
  LIVE.running = false; LIVE.win = null;
  $('survey').textContent = 'Survey live'; $('survey').classList.remove('on'); $('survey').disabled = false;
  refresh(); draw();
}
$('survey').addEventListener('click', async () => {
  if (!LIVE.running) { startSurvey(); return; }
  $('survey').disabled = true; $('surveyNote').textContent = 'stopping after this window…';
  try { await fetch('/api/survey/stop', {method: 'POST'}); } catch (e) { /* rxapp restarting */ }
});

// ------------------------------------------------------------------ load
(async () => {
  try {
    const l = await (await fetch('/api/survey/last?page=lband')).json();
    if (l && l.av) { merge(l.range_mhz[0], 0, l.av, l.mx); LIVE.last = l; }
  } catch (e) { /* none yet */ }
  const zb = [{key: 'all', label: '1000–1700 MHz', range: [LO, HI]},
    ...OPPS.filter((o) => o.verdict !== 'no').map((o) => ({key: o.id, label: `${o.freq.split(' ')[0]} ${o.title.split(' (')[0]}`, range: o.zoom}))];
  $('zooms').innerHTML = zb.map((z) => `<button type="button" data-key="${z.key}" aria-pressed="false">${esc(z.label)}</button>`).join('');
  $('zooms').addEventListener('click', (e) => { const b = e.target.closest('button[data-key]'); if (b) setView(zb.find((z) => z.key === b.dataset.key)); });
  refresh(); setView(zb[0]);
})();

// the spectrum tile (minispec.js): while a live survey runs, its whole range with the window being swept highlighted
window.miniSpecRange = () => (LIVE.running && miniSpec.scan() ? miniSpec.scanRange('L-band survey') : null);
