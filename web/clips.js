// SPDX-License-Identifier: GPL-3.0-or-later
// Recorded-transmission player for the airband and public-safety logs: a play button on every transcript line,
// a progress bar along the playing line, and "play on" (when a clip ends, the next transmission in time order
// plays). Playing a clip stops the page's live listening and any other page's audio (audioFocus).
//   const clips = clipPlayer({url: (r) => '/api/.../clip/...', next: (r) => r2 | null, onStart: () => listenOff(),
//                             list: '#log'})
//   clips.button(r)        -> HTML for a log line's play button (put it inside <li data-i="...">)
//   clips.toggle(r)        -> play r, or stop it if it is playing
//   clips.playing          -> the record playing now, or null
'use strict';

(function () {
  const st = document.createElement('style');
  st.textContent = `
.clipplay{all:unset;box-sizing:border-box;display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;
  border:1px solid var(--line,#ccc);color:var(--ink,#222);background:var(--panel,#fff);cursor:pointer;font-size:9px;line-height:1;
  flex:none;vertical-align:middle;position:relative}
.clipplay:hover{border-color:var(--accent,#2E7B57);color:var(--accent,#2E7B57)}
.clipplay:focus-visible{outline:2px solid var(--accent,#2E7B57);outline-offset:1px}
.clipplay.on{background:var(--accent,#2E7B57);border-color:var(--accent,#2E7B57);color:#fff}
li.clip-on{background-image:linear-gradient(90deg,color-mix(in srgb,var(--accent,#2E7B57) 14%,transparent) var(--clip-p,0%),transparent var(--clip-p,0%))}
.playon{display:inline-flex;align-items:center;gap:5px;font:500 12px "IBM Plex Mono",monospace;color:var(--muted,#666);cursor:pointer;user-select:none}`;
  document.head.appendChild(st);
})();

function clipPlayer(opts) {
  const P = {playing: null, audio: null, playOn: false};
  const lists = () => [...document.querySelectorAll(opts.list || '#log')];      // e.g. '#log, #incTl'

  function paint() {
    for (const L of lists()) paintIn(L);
  }

  function paintIn(L) {
    L.querySelectorAll('li.clip-on').forEach((x) => { x.classList.remove('clip-on'); x.style.removeProperty('--clip-p'); });
    L.querySelectorAll('.clipplay.on').forEach((b) => { b.classList.remove('on'); b.textContent = '▶'; b.title = 'Play this transmission'; });
    const el = P.playing && L.querySelector(`li[data-i="${P.playing.i}"]`);
    if (!el) return;
    el.classList.add('clip-on');
    const a = P.audio;
    if (a && a.duration) el.style.setProperty('--clip-p', `${(100 * a.currentTime / a.duration).toFixed(1)}%`);
    const b = el.querySelector('.clipplay');
    if (b) { b.classList.add('on'); b.textContent = '■'; b.title = 'Stop'; }
  }

  function stop(quiet) {
    if (P.audio) { P.audio.onended = null; P.audio.pause(); }
    P.audio = null; P.playing = null;
    if (!quiet) audioFocus.release();
    paint();
    opts.onState && opts.onState(null);
  }

  function play(r) {
    if (P.audio) { P.audio.onended = null; P.audio.pause(); }
    opts.onStart && opts.onStart();
    audioFocus.claim(() => stop(true));
    const a = new Audio(opts.url(r));
    P.audio = a; P.playing = r;
    a.ontimeupdate = paint;
    a.onended = () => {
      const n = P.playOn && opts.next ? opts.next(r) : null;
      if (n) play(n); else stop();
    };
    a.onerror = () => { if (P.audio === a) stop(); };
    a.play().catch(() => { if (P.audio === a) stop(); });
    paint();
    opts.onState && opts.onState(r);
  }

  // the page redraws its log as transmissions arrive: mark the playing line again
  for (const L0 of lists()) new MutationObserver(() => { if (P.playing) paintIn(L0); }).observe(L0, {childList: true});

  return {
    get playing() { return P.playing; },
    button: (r) => `<button type="button" class="clipplay${P.playing && P.playing.i === r.i ? ' on' : ''}" data-play="${r.i}"
      title="${P.playing && P.playing.i === r.i ? 'Stop' : 'Play this transmission'}" aria-label="Play">${P.playing && P.playing.i === r.i ? '■' : '▶'}</button>`,
    toggle(r) { if (P.playing && P.playing.i === r.i) stop(); else play(r); },
    play, stop: () => stop(), paint,
    set playOn(v) { P.playOn = !!v; }, get playOn() { return P.playOn; },
  };
}
