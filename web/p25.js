// SPDX-License-Identifier: GPL-3.0-or-later
// P25, Live: the 700 MHz trunked system as dsd-fme decodes it (rxapp/p25.py): talkgroups, grants, recorded calls.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error(r.statusText); return r.json(); };
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const hms = (t) => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? `${Math.max(0, Math.round(s))} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`; };
const mhz = (h) => h ? (h / 1e6).toFixed(5) : '–';

const S = {session: null, live: false, trunk: {}, calls: [], tg: null, playing: null, newest: null, queue: []};
const player = $('player');

function drawFacts() {
  const t = S.trunk, sys = t.system || {};
  $('fSys').textContent = sys.wacn ? `WACN ${sys.wacn} · SYS ${sys.sysid} · NAC ${sys.nac} · site ${parseInt(sys.site, 10)}` : '–';
  $('fCc').textContent = !t.running ? 'not decoding' : t.locked ? `locked · ${mhz(t.cc_hz)}` : `searching · ${mhz(t.cc_hz)}`;
  const act = (t.talkgroups || []).find((g) => g.active);
  $('fNow').textContent = !t.running ? '–' : t.on_voice ? `voice ${mhz(t.tuned_hz)}${act ? ' · TG ' + act.tg : ''}` : act ? `TG ${act.tg} granted` : 'quiet';
}

// Reset stats: talkgroups heard since the reset, their calls counted from then (g.times: the hour's call starts)
const counted = (t) => !window.statsReset || statsReset.keep(t);
function drawTgs() {
  const tgs = (S.trunk.talkgroups || []).filter((g) => counted(g.last))
    .map((g) => (window.statsReset && statsReset.since() && g.times ? {...g, grants: g.times.filter(counted).length} : g));
  $('tgN').textContent = tgs.length ? `${tgs.length} heard · ${tgs.filter((g) => g.enc).length} encrypted` : '';
  $('tgs').innerHTML = tgs.map((g) => `<tr data-tg="${g.tg}" class="${S.tg === g.tg ? 'sel' : ''}">
      <td>${g.tg}</td><td>${g.active ? '<span class="badge b-live">on air</span>' : ago(g.last)}</td><td class="num">${g.grants}</td>
      <td>${mhz(g.hz)}</td><td>${g.src ?? '<span class="muted">–</span>'}</td>
      <td>${g.enc ? '<span class="badge b-enc" title="Encrypted: assigned in the clear, can\'t be heard">encrypted</span>' : ''}</td></tr>`).join('')
    || `<tr><td colspan="6" class="empty">${S.trunk.running ? 'No calls yet. The control channel assigns a voice channel for every call; they appear here as they happen.' : 'Nothing decoded in this session.'}</td></tr>`;
}

// voice_session.py transcribes each clear call with Whisper; encrypted ones are skipped (they decode to noise)
function said(c) {
  if (c.enc) return '<td class="said none">encrypted</td>';
  if (!c.transcribed) return `<td class="said none">${c.secs < 0.8 ? '' : 'transcribing…'}</td>`;
  return c.text ? `<td class="said" title="${esc(c.text_hint && c.text_hint !== c.text ? 'with the dispatch vocabulary hint: ' + c.text_hint : '')}">${esc(c.text)}</td>`
    : '<td class="said none">no words</td>';
}
function drawCalls() {
  const showEnc = $('showEnc').checked;
  const list = (S.tg == null ? S.calls : S.calls.filter((c) => c.tg === S.tg)).filter((c) => showEnc || !c.enc);
  $('clearTg').style.display = S.tg == null ? 'none' : '';
  $('clearTg').textContent = `TG ${S.tg} only · show all`;
  $('calls').innerHTML = list.slice(0, 300).map((c) => `<tr ${c.enc ? '' : `data-clip="${esc(c.clip)}"`} class="${S.playing === c.clip ? 'playing' : ''}${c.enc ? ' muted' : ''}">
      <td>${hms(c.t)}</td><td>${c.tg}${c.enc ? ' <span class="badge b-enc" title="Probably encrypted (AES): likely noise. The marking is a heuristic and can be wrong">maybe encrypted</span>' : ''}</td><td>${c.src}</td><td class="num">${c.secs.toFixed(1)} s</td>
      ${said(c)}</tr>`).join('')
    || `<tr><td colspan="5" class="empty">${S.tg == null ? 'No unencrypted calls recorded in this session yet.' : 'No recorded calls for this talkgroup (it may be encrypted).'}</td></tr>`;
}

function drawGrants() {
  $('grants').innerHTML = (S.trunk.grants || []).filter((g) => counted(g.t)).map((g) => `<li><span class="t">${hms(g.t)}</span>TG ${g.tg} → ${mhz(g.hz)}${g.src ? ' · radio ' + g.src : ''}${g.enc ? ' · encrypted' : ''}</li>`).join('')
    || '<li class="muted">No grants yet.</li>';
}

function play(clip) {
  liveOff();
  S.playing = clip;
  player.src = `/api/p25/clip/${encodeURIComponent(S.session)}/${encodeURIComponent(clip)}`;
  player.play().catch(() => { S.playing = null; drawCalls(); });
  audioFocus.claim(() => { player.pause(); S.queue = []; $('auto').checked = false; S.playing = null; drawCalls(); });
  drawCalls();
}
player.addEventListener('ended', () => {
  S.playing = null;
  if (S.queue.length) play(S.queue.shift()); else { audioFocus.release(); drawCalls(); }
});
player.addEventListener('pause', () => { if (!player.ended) { audioFocus.release(); } });

$('calls').addEventListener('click', (e) => { const tr = e.target.closest('tr[data-clip]'); if (tr) { S.queue = []; play(tr.dataset.clip); } });
$('tgs').addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-tg]'); if (!tr) return;
  const tg = Number(tr.dataset.tg);
  S.tg = S.tg === tg ? null : tg; drawTgs(); drawCalls();
});
$('clearTg').addEventListener('click', () => { S.tg = null; drawTgs(); drawCalls(); });
$('showEnc').addEventListener('change', drawCalls);
if (window.statsReset) statsReset.onChange(() => poll());
// "play new calls" is listening live: it switches the receiver here (focus.js); unticking doesn't release it
$('auto').addEventListener('change', () => { if ($('auto').checked && window.receiverFocus) receiverFocus.ensure((f) => f === 'p25' || f === 'window:p25'); });

// ------------------------------------------------------------------ live (rxapp/p25live.py)
// dsd-fme follows the trunk to each clear call; Listen plays what it decodes. "Follow activity" stays with the
// call it is playing and moves on when that goes quiet; a talkgroup plays only that one.
const live = {ctx: null, gain: null, ws: null, next: 0, on: false, heard: 0};
function liveConnect() {
  if (live.ws) { const w = live.ws; live.ws = null; w.onmessage = w.onclose = null; w.close(); }
  if (!live.on) return;
  const tg = $('lchan').value;
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/p25/audio' + (tg ? `?tg=${tg}` : ''));
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (e) => {
    const rate = new DataView(e.data).getUint32(0, true), pcm = new Int16Array(e.data, 4);
    if (!pcm.length) return;
    let loud = 0; for (let i = 0; i < pcm.length; i += 8) loud = Math.max(loud, Math.abs(pcm[i]));
    if (loud > 200) live.heard = Date.now();
    const buf = live.ctx.createBuffer(1, pcm.length, rate), out = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 32768;
    const src = live.ctx.createBufferSource(); src.buffer = buf; src.connect(live.gain);
    const now = live.ctx.currentTime;
    if (live.next < now + 0.05 || live.next > now + 1.0) live.next = now + 0.25;
    src.start(live.next); live.next += buf.duration;
  };
  ws.onclose = () => { if (live.ws === ws && live.on) setTimeout(liveConnect, 1000); };
  live.ws = ws;
}
function liveOff() {                     // another page or a recorded call started playing
  if (!live.on) return;
  live.on = false; liveConnect(); drawLive();
}
$('listen').addEventListener('click', () => {
  live.on = !live.on;
  if (live.on) {
    player.pause(); S.queue = []; $('auto').checked = false;
    audioFocus.claim(liveOff);
    if (window.receiverFocus) receiverFocus.ensure((f) => f === 'p25' || f === 'window:p25');
    if (!live.ctx) { live.ctx = new AudioContext(); live.gain = live.ctx.createGain(); live.gain.connect(live.ctx.destination); }
    live.ctx.resume(); live.gain.gain.value = +$('vol').value;
  } else audioFocus.release();
  liveConnect(); drawLive();
});
$('lchan').addEventListener('change', () => { $('lchan').blur(); liveConnect(); drawLive(); });
$('vol').addEventListener('input', () => { if (live.gain) live.gain.gain.value = +$('vol').value; });
function drawLive() {
  $('listen').textContent = live.on ? 'Stop' : 'Listen';
  $('listen').classList.toggle('on', live.on);
  const tgs = (S.trunk.talkgroups || []).filter((g) => !g.enc).sort((a, b) => b.grants - a.grants);
  const keep = $('lchan').value;
  const opts = '<option value="">Follow activity</option>' + tgs.map((g) => `<option value="${g.tg}">TG ${g.tg} · ${g.grants} call${g.grants === 1 ? '' : 's'}</option>`).join('')
    + (keep && !tgs.some((g) => String(g.tg) === keep) ? `<option value="${esc(keep)}">TG ${esc(keep)}</option>` : '');
  if ($('lchan').innerHTML !== opts) { $('lchan').innerHTML = opts; $('lchan').value = keep; }
  const slots = Object.values(S.trunk.slots || {}).filter((x) => !x.enc);
  const talking = Date.now() - live.heard < 1500;
  const latest = S.calls.find((c) => c.text && (!keep || String(c.tg) === keep));
  $('lnow').innerHTML = !live.on ? (slots.length ? `on air now: ${slots.map((x) => 'TG ' + x.tg).join(', ')}` : '')
    : (talking ? `<b>● playing</b> ${slots.length ? slots.map((x) => 'TG ' + x.tg).join(', ') : ''}` : 'waiting for a clear call…')
      + (latest ? ` · last heard ${hms(latest.t)}: “${esc(latest.text.slice(0, 120))}”` : '');
}
setInterval(drawLive, 500);

async function loadSession(name) {
  const info = await api('/api/p25' + (name ? `?session=${encodeURIComponent(name)}` : ''));
  S.session = info.session; S.newest = null;
  $('session').innerHTML = info.sessions.map((s) => `<option ${s === info.session ? 'selected' : ''}>${esc(s)}</option>`).join('');
  await poll();
}

async function poll() {
  try {
    const q = `?session=${encodeURIComponent(S.session)}`;
    const info = await api('/api/p25' + q);
    const st = info.status || {}; S.live = info.live;
    $('fLive').className = 'live' + (S.live && st.listening ? '' : ' off');
    $('fLive').lastChild.textContent = !S.live ? 'recorded' : st.listening ? 'decoding' : `away on ${st.window}`;
    const [trunk, calls] = await Promise.all([api('/api/p25/trunk' + q), api('/api/p25/calls' + q)]);
    S.trunk = trunk; S.calls = calls.filter((c) => counted(c.t));       // Reset stats: calls after it
    const err = trunk.error || (st.p25 && st.p25.error);
    const notice = !S.live ? 'No P25 session is running: start voice_session.py with a p25 window (see rxapp/README.md).'
      : err ? err
      : !st.listening ? 'The P25 window runs only while the receiver is held here: press “Switch receiver here” above. (It is left out of the normal rotation.)'
      : trunk.running && !trunk.locked ? 'Looking for the control channel…' : '';
    $('notice').textContent = notice; $('notice').style.display = notice ? '' : 'none';
    // play new calls as they finish
    const top = calls.length ? calls[0].clip : null;
    if (S.newest !== null && $('auto').checked) {
      const fresh = [];
      for (const c of calls) { if (c.clip === S.newest) break; if (!c.enc && (S.tg == null || c.tg === S.tg)) fresh.push(c.clip); }
      S.queue.push(...fresh.reverse());
      if (!S.playing && S.queue.length) play(S.queue.shift());
    }
    S.newest = top;
    drawFacts(); drawTgs(); drawCalls(); drawGrants(); drawLive();
  } catch (e) { $('notice').textContent = `No P25 session yet (${e.message}).`; $('notice').style.display = ''; }
}

$('session').addEventListener('change', () => loadSession($('session').value));
loadSession().catch(() => poll()).finally(() => setInterval(poll, 2000));

// the spectrum tile (minispec.js): the system's control and voice channels; the channel being decoded marked
window.miniSpecRange = () => {
  const t = S.trunk || {}, marks = [{f: 772.30625e6, label: 'control', color: '#4cd08a'}];
  if (t.on_voice && t.tuned_hz) {
    marks.push(live.on ? {f: t.tuned_hz, play: true, width: 12500, label: $('lchan').value ? `TG ${$('lchan').value}` : 'following'}
      : {f: t.tuned_hz, label: 'voice'});
  }
  const f = t.on_voice && t.tuned_hz ? t.tuned_hz : 772.30625e6;
  const zoom = {lo: f - 60e3, hi: f + 60e3, label: `${(f / 1e6).toFixed(5)} MHz · ${f === 772.30625e6 ? 'control channel' : 'voice channel'}`, marks};
  return {lo: 771.8e6, hi: 775.2e6, label: 'P25 system · 771.8–775.2 MHz', marks, zoom};
};
