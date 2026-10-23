// SSTV Pictures: the SSTV monitor's frequencies (live state, detector, listen) and its picture gallery.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p, opts) => { const r = await fetch(p, opts); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); };
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const when = (t) => new Date(t * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
const mhz = (f) => (f / 1e6).toFixed(3);
const SYNC_TRIGGER = 0.25;            // sstvmon.looks_like_sstv

const S = {info: null, pics: [], ids: null, health: null, sel: null, listen: null, playing: null};

// ------------------------------------------------------------------ frequencies
function drawRows() {
  const i = S.info;
  $('toggle').textContent = i.running ? 'Stop monitor' : 'Start monitor';
  $('fMon').className = 'live' + (i.running ? '' : ' off');
  $('fMon').lastChild.textContent = i.running ? 'running' : 'stopped';
  const hf = S.health && S.health.band === 'hf';
  $('fRx').innerHTML = !S.health ? '–' : hf ? 'on HF: listening' : '<span class="away">on VHF: SSTV paused until the HF input is back</span>';
  $('rows').innerHTML = i.channels.map((c) => {
    const sync = c.last && c.last.sync != null ? c.last.sync : null;
    const cap = c.state.startsWith('capturing');
    return `<tr class="${cap ? 'cap' : ''}">
      <td>${esc(c.band)}</td><td>${mhz(c.freq_hz)} ${c.mode.toUpperCase()}</td>
      <td>${c.parked ? '<span class="away">paused (VHF)</span>' : cap ? `<span class="capture">● ${esc(c.state)}</span>` : esc(c.state)}</td>
      <td>${sync == null ? '' : `<span class="meter" title="sync ${sync.toFixed(2)}, video ${c.last.video.toFixed(2)}"><b style="width:${Math.min(100, sync / 0.6 * 100)}%"></b><i style="left:${SYNC_TRIGGER / 0.6 * 100}%"></i></span>
        ${sync.toFixed(2)}${c.last.mode && c.last.mode !== '?' && sync >= SYNC_TRIGGER ? ' · ' + esc(c.last.mode) : ''}`}</td>
      <td>${c.triggers}</td><td>${c.pictures}</td>
      <td><button class="smallbtn" data-ssrc="${c.ssrc}" data-f="${c.freq_hz}" ${c.parked ? 'disabled' : ''}>${S.listen === c.ssrc ? 'Stop' : 'Listen'}</button></td></tr>`;
  }).join('') || '<tr><td colspan="7" class="empty">The monitor isn\'t running. Start it to watch 3.845, 7.171, 14.230, 14.233, 21.340 and 28.680 MHz.</td></tr>';
}
$('toggle').addEventListener('click', async () => {
  $('toggle').disabled = true;
  try { S.info = await api('/api/sstv', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({on: !S.info.running})}); }
  catch (e) { alert(e.message); }
  $('toggle').disabled = false;
  drawRows();
});

// ------------------------------------------------------------------ listen to one frequency (SSTV sounds like warbling tones)
const audio = {ctx: null, gain: null, ws: null, next: 0};
function stopListen() {
  if (audio.ws) { const w = audio.ws; audio.ws = null; w.onmessage = w.onclose = null; w.close(); }
  S.listen = null; $('now').textContent = ''; $('listen').disabled = true; $('listen').textContent = 'Listen';
}
function startListen(ssrc, f) {
  stopListen();
  if (!audio.ctx) { audio.ctx = new AudioContext(); audio.gain = audio.ctx.createGain(); audio.gain.connect(audio.ctx.destination); }
  audio.ctx.resume(); audio.gain.gain.value = +$('vol').value; audio.next = 0;
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
  audio.ws = ws; S.listen = ssrc;
  $('now').textContent = `playing ${mhz(f)} MHz`; $('listen').disabled = false; $('listen').textContent = 'Stop listening';
}
$('rows').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-ssrc]'); if (!b) return;
  if (S.listen === +b.dataset.ssrc) stopListen(); else startListen(+b.dataset.ssrc, +b.dataset.f);
  drawRows();
});
$('listen').addEventListener('click', () => { stopListen(); drawRows(); });
$('vol').addEventListener('input', () => { if (audio.gain) audio.gain.gain.value = +$('vol').value; });

// ------------------------------------------------------------------ receiving now
function drawLive() {
  const live = (S.info.channels || []).filter((c) => c.live);
  const capturing = (S.info.channels || []).filter((c) => c.state.startsWith('capturing') && !c.live);
  if (!live.length) {
    $('live').innerHTML = `<p class="empty">${capturing.length
      ? `Capturing on ${capturing.map((c) => esc(c.band)).join(', ')}: waiting for the opening tone burst that names the mode before the picture can be drawn.`
      : 'Nothing being received right now. When a frequency’s detector passes the mark, its picture appears here and builds up line by line.'}</p>`;
    return;
  }
  // keep the <img> elements, so the picture doesn't flash on every update
  const have = new Set([...$('live').querySelectorAll('.livepic')].map((d) => d.dataset.band));
  if (live.some((c) => !have.has(c.band)) || have.size !== live.length) {
    $('live').innerHTML = live.map((c) => `<div class="livepic" data-band="${esc(c.band)}">
        <img alt="SSTV picture being received on ${esc(c.band)}"><div class="prog"><b></b></div><div class="cap"></div></div>`).join('');
  }
  for (const c of live) {
    const d = $('live').querySelector(`.livepic[data-band="${CSS.escape(c.band)}"]`);
    const L = c.live, pct = Math.round(100 * L.rows / L.height);
    const img = d.querySelector('img'), url = `/api/sstv/live/${encodeURIComponent(c.band)}.png?t=${L.updated}`;
    if (img.dataset.src !== url) { img.dataset.src = url; img.src = url; }
    d.querySelector('.prog b').style.width = pct + '%';
    d.querySelector('.cap').innerHTML = `<span>● ${esc(c.band)} ${mhz(c.freq_hz)} MHz · ${esc(L.mode.replace(/_/g, ' '))}</span>
      <span>${L.rows}/${L.height} lines (${pct}%) · ${L.seconds} s</span>`;
  }
}

// ------------------------------------------------------------------ gallery + viewer
function drawGallery() {
  const band = $('fBand').value;
  const pics = S.pics.filter((p) => !band || p.band === band);
  $('fPics').textContent = S.pics.length;
  $('fLast').textContent = S.pics.length ? when(S.pics[0].t) : '–';
  $('count').textContent = pics.length ? `${pics.length} picture${pics.length > 1 ? 's' : ''}` : '';
  const bands = [...new Set(S.pics.map((p) => p.band))].sort();
  const prev = $('fBand').value;
  $('fBand').innerHTML = '<option value="">all bands</option>' + bands.map((b) => `<option>${esc(b)}</option>`).join('');
  $('fBand').value = bands.includes(prev) ? prev : '';
  $('gal').innerHTML = pics.map((p) => `<figure data-id="${p.id}">
      <img src="/api/sstv/pictures/${p.id}.png" alt="SSTV picture, ${esc(p.mode)}, ${when(p.t)}" loading="lazy">
      <figcaption><span>${when(p.t)}</span><span>${mhz(p.freq_hz)} · ${esc(p.mode.replace(/_/g, ' '))}${p.complete ? '' : ' · <span class="partial">partial</span>'}</span></figcaption>
    </figure>`).join('')
    || '<p class="empty">No pictures yet. SSTV is busiest on weekend daytimes, mostly on 14.230 MHz (20 m). Pictures appear here as soon as they are decoded; the monitor also needs the receiver on the HF input (use “Switch receiver here” to keep it there).</p>';
}
$('fBand').addEventListener('change', drawGallery);
$('gal').addEventListener('click', (e) => { const f = e.target.closest('figure[data-id]'); if (f) openPic(S.pics.find((p) => p.id === +f.dataset.id)); });
function openPic(p) {
  S.sel = p;
  $('lbImg').src = `/api/sstv/pictures/${p.id}.png`;
  $('lbImg').alt = `SSTV picture, ${p.mode}`;
  $('lbMeta').textContent = `${when(p.t)} · ${p.band} ${mhz(p.freq_hz)} MHz · ${p.mode.replace(/_/g, ' ')} · ${p.width}×${p.height}${p.complete ? '' : ' · partial (the signal faded)'}`;
  $('lbWav').href = `/api/sstv/pictures/${p.id}.wav`;
  $('lb').classList.add('on');
}
function closePic() { $('lb').classList.remove('on'); if (S.playing) S.playing.pause(); }
$('lbClose').addEventListener('click', closePic);
$('lb').addEventListener('click', (e) => { if (e.target === $('lb')) closePic(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePic(); });
$('lbPlay').addEventListener('click', () => { if (S.playing) S.playing.pause(); S.playing = new Audio(`/api/sstv/pictures/${S.sel.id}.wav`); S.playing.play(); });
$('lbDel').addEventListener('click', async () => {
  if (!S.sel || !confirm('Delete this picture and its audio?')) return;
  await api(`/api/sstv/pictures/${S.sel.id}`, {method: 'DELETE'});
  closePic(); S.ids = null; refresh();
});

// ------------------------------------------------------------------ polling
async function refresh() {
  try {
    [S.info, S.health] = await Promise.all([api('/api/sstv'), api('/api/health')]);
    drawRows(); drawLive();
    const pics = await api('/api/sstv/pictures?limit=500');
    const ids = pics.map((p) => p.id).join();
    if (ids !== S.ids) { S.ids = ids; S.pics = pics; drawGallery(); }
  } catch (e) { $('fMon').lastChild.textContent = 'rxapp not reachable'; }
}
refresh(); setInterval(refresh, 3000);
