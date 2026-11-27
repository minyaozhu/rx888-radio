// SPDX-License-Identifier: GPL-3.0-or-later
// P25, Live: the 700 MHz trunked system as dsd-fme decodes it (rxapp/p25.py): talkgroups, grants, recorded calls.
'use strict';

const $ = (id) => document.getElementById(id);
const api = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error(r.statusText); return r.json(); };
const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
const hms = (t) => new Date(t * 1000).toLocaleTimeString([], {hour12: false});
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? `${Math.max(0, Math.round(s))} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`; };
const mhz = (h) => h ? (h / 1e6).toFixed(5) : '–';

const S = {session: null, live: false, trunk: {}, calls: [], threads: [], tg: null, playing: null, newest: null, queue: [], openTg: new Set()};
const player = $('player');

function drawFacts() {
  const t = S.trunk, sys = t.system || {};
  $('fSys').textContent = sys.wacn ? `WACN ${sys.wacn} · SYS ${sys.sysid} · NAC ${sys.nac} · site ${parseInt(sys.site, 10)}` : '–';
  $('fCc').textContent = !t.running ? 'not decoding' : t.locked ? `locked · ${mhz(t.cc_hz)}` : `searching · ${mhz(t.cc_hz)}`;
  const act = (t.talkgroups || []).find((g) => g.active);
  const v = t.voice || [], on = v.filter((x) => x.active);
  $('fNow').textContent = !t.running ? '–'
    : `${on.length ? `${on.length} call${on.length === 1 ? '' : 's'} on ${on.map((x) => mhz(x.hz)).join(', ')}` : act ? `TG ${act.tg} granted` : 'quiet'}`
      + ` · ${v.length} voice decoder${v.length === 1 ? '' : 's'}`;
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
  if (c.enc) return '<span class="said none">encrypted</span>';
  if (!c.transcribed) return `<span class="said none">${c.secs < 0.8 ? '' : 'transcribing…'}</span>`;
  return c.text ? `<span class="said" title="${esc(c.text_hint && c.text_hint !== c.text ? 'with the dispatch vocabulary hint: ' + c.text_hint : '')}">${esc(c.text)}</span>`
    : '<span class="said none">no words</span>';
}
// the talkgroup live listening is following now (null when not listening or nothing clear is on the air)
function followTg() {
  if (!live.on) return null;
  if ($('lchan').value) return Number($('lchan').value);
  const slots = Object.values(S.trunk.slots || {}).filter((x) => !x.enc).sort((a, b) => (a.age ?? 99) - (b.age ?? 99));
  return Date.now() - live.heard < 6000 && slots.length ? slots[0].tg : null;
}
// Conversations grouped by talkgroup: one block per talkgroup (the most recently heard first), its conversations
// oldest to newest like a chat, with the time between them. The last SHOW_CONV are shown, older ones on request.
const SHOW_CONV = 4;
const gapText = (s) => s < 90 ? `${Math.round(s)} s later` : s < 5400 ? `${Math.round(s / 60)} min later` : `${(s / 3600).toFixed(1)} h later`;
function convHtml(th, showEnc) {
  const span = hms(th.start) + (th.n > 1 ? '–' + hms(th.end) : '');
  const clear = th.calls.filter((c) => !c.enc);
  return `<div class="conv" data-th="${esc(th.id)}">
      <div class="conv-head"><span class="muted">${span} · ${th.n} call${th.n === 1 ? '' : 's'} · radio${th.srcs.length === 1 ? '' : 's'} ${th.srcs.join(', ')}</span>
        ${th.live ? '<span class="badge b-live">live</span>' : ''}
        ${clear.length ? `<button type="button" class="mini" data-playth="${esc(th.id)}" title="Play this conversation">▶</button>` : ''}</div>
      <ol class="overs">${th.calls.filter((c) => showEnc || !c.enc).map((c) => `<li ${c.enc ? 'class="enc"' : `data-clip="${esc(c.clip)}"${S.playing === c.clip ? ' class="playing"' : ''}`}>
        <span class="t">${hms(c.t)}</span><span class="src">${c.src}</span>${said(c)}<span class="len">${c.secs.toFixed(1)} s</span></li>`).join('')}</ol>
    </div>`;
}
function drawCalls() {
  const showEnc = $('showEnc').checked, ftg = followTg();
  $('clearTg').style.display = S.tg == null ? 'none' : '';
  $('clearTg').textContent = `TG ${S.tg} only · show all`;
  const groups = new Map();                      // talkgroup -> its conversations, newest first (as served)
  for (const th of S.threads) {
    if ((S.tg != null && th.tg !== S.tg) || (!showEnc && th.enc)) continue;
    if (!groups.has(th.tg)) groups.set(th.tg, []);
    groups.get(th.tg).push(th);
  }
  const list = [...groups.entries()].slice(0, 60);       // already in order of the latest conversation
  $('threads').innerHTML = list.map(([tg, convs]) => {
    const all = S.openTg.has(tg), shown = (all ? convs : convs.slice(0, SHOW_CONV)).slice().reverse();
    const n = convs.reduce((k, th) => k + th.n, 0), fol = ftg === tg, live = convs[0].live;
    const parts = [];
    shown.forEach((th, i) => {
      if (i) parts.push(`<div class="gap">${gapText(th.start - shown[i - 1].end)}</div>`);
      parts.push(convHtml(th, showEnc));
    });
    return `<article class="thread${fol ? ' following' : ''}" data-tg="${tg}">
      <div class="th-head"><span class="tg">TG ${tg}</span>
        ${fol ? '<span class="badge b-follow">following</span>' : live ? '<span class="badge b-live">live</span>' : ''}
        ${convs.every((th) => th.enc) ? '<span class="badge b-enc" title="Probably encrypted (AES): likely noise">maybe encrypted</span>' : ''}
        <span class="muted">${convs.length} conversation${convs.length === 1 ? '' : 's'} · ${n} call${n === 1 ? '' : 's'} · last ${hms(convs[0].end)}</span></div>
      ${convs.length > shown.length ? `<button type="button" class="older" data-older="${tg}">${convs.length - shown.length} earlier conversation${convs.length - shown.length === 1 ? '' : 's'}</button>`
        : all && convs.length > SHOW_CONV ? `<button type="button" class="older" data-older="${tg}">show fewer</button>` : ''}
      ${parts.join('')}
    </article>`;
  }).join('')
    || `<p class="empty">${S.tg == null ? 'No unencrypted calls recorded in this session yet.' : 'No recorded calls for this talkgroup (it may be encrypted).'}</p>`;
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

$('threads').addEventListener('click', (e) => {
  const o = e.target.closest('button[data-older]');
  if (o) { const tg = Number(o.dataset.older); if (S.openTg.has(tg)) S.openTg.delete(tg); else S.openTg.add(tg); drawCalls(); return; }
  const b = e.target.closest('button[data-playth]');
  if (b) {
    const th = S.threads.find((x) => x.id === b.dataset.playth);
    const clips = th ? th.calls.filter((c) => !c.enc).map((c) => c.clip) : [];
    if (clips.length) { S.queue = clips.slice(1); play(clips[0]); }
    return;
  }
  const li = e.target.closest('li[data-clip]'); if (li) { S.queue = []; play(li.dataset.clip); }
});
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
// lead: how far ahead audio is scheduled. It starts at 0.25 s and grows (to 1 s) each time a piece arrives too late
// to play on time (a gap), so a jittery link (remote, through Funnel) stops stuttering; it eases back after a quiet minute.
const LEAD_MIN = 0.25, LEAD_MAX = 1.0;
const live = {ctx: null, gain: null, ws: null, next: 0, on: false, heard: 0, lead: LEAD_MIN, lastRx: 0, lastLate: 0};
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
    const now = live.ctx.currentTime, gap = now - live.lastRx;
    live.lastRx = now;
    if (live.next < now + 0.02) {
      // ran dry: mid-stream (pieces were arriving) it was a late piece, so keep more in reserve from now on
      if (gap < 0.5) { live.lead = Math.min(LEAD_MAX, live.lead + 0.15); live.lastLate = now; }
      live.next = now + live.lead;
    } else if (live.next > now + live.lead + 1.0) live.next = now + live.lead;     // too far behind: catch up
    if (now - live.lastLate > 60 && live.lead > LEAD_MIN) { live.lead = Math.max(LEAD_MIN, live.lead - 0.05); live.lastLate = now; }
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
  const ftg = followTg();
  const latest = S.calls.find((c) => c.text && (ftg != null ? c.tg === ftg : !keep || String(c.tg) === keep));
  $('lnow').innerHTML = !live.on ? (slots.length ? `on air now: ${slots.map((x) => 'TG ' + x.tg).join(', ')}` : '')
    : (talking ? `<b>● playing</b> ${ftg != null ? 'TG ' + ftg + ' conversation' : slots.map((x) => 'TG ' + x.tg).join(', ')}`
      : ftg != null ? `following TG ${ftg}, waiting for a reply…` : 'waiting for a clear call…')
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
    const [trunk, threads] = await Promise.all([api('/api/p25/trunk' + q), api('/api/p25/threads' + q)]);
    S.trunk = trunk;
    S.threads = threads.map((th) => ({...th, calls: th.calls.filter((c) => counted(c.t))})).filter((th) => th.calls.length);   // Reset stats
    const calls = S.threads.flatMap((th) => th.calls).sort((a, b) => b.t - a.t);
    S.calls = calls;
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
      // the conversation being played first: its new replies go ahead of other talkgroups' calls
      const tgOf = (clip) => (S.calls.find((c) => c.clip === clip) || {}).tg, cur = tgOf(S.playing);
      if (cur != null) S.queue.sort((a, b) => (tgOf(b) === cur) - (tgOf(a) === cur));
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
