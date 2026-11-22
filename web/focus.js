// SPDX-License-Identifier: GPL-3.0-or-later
// "Switch the receiver here": hold the voice session (voice_session.py) on this page's windows, or release.
// Include with <script src="focus.js" data-focus="air|safety|hf|pause" data-what="..."></script> and an
// element with id="focusBox" where the button goes.
// Listening implies the receiver: a page's Listen calls receiverFocus.ensure(isHere), which switches the receiver
// here unless it already is (isHere(focus), default: focus === data-focus). Never the other way: holding the
// receiver doesn't start audio, and stopping audio doesn't release the receiver.
'use strict';
(function () {
  const me = document.currentScript;
  const want = me.dataset.focus, what = me.dataset.what || 'this page';
  const box = document.getElementById('focusBox');
  if (!box) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.style.cssText = 'font:600 12px/1 system-ui,sans-serif;letter-spacing:.04em;padding:7px 11px;border-radius:4px;cursor:pointer;border:1px solid currentColor;background:transparent;color:inherit';
  const note = document.createElement('span');
  note.style.cssText = 'margin-left:8px;font:12px ui-monospace,Menlo,monospace;opacity:.75';
  box.append(btn, note);
  let state = {focus: null, label: ''};
  function render() {
    const mine = state.focus === want;
    btn.textContent = mine ? (want === 'pause' ? '■ Voice session paused · Resume' : '● Receiver held here · Release')
      : (want === 'pause' ? 'Pause voice session (manual tuning)' : 'Switch receiver here');
    btn.style.background = mine ? '#A3316F' : 'transparent';
    btn.style.color = mine ? '#fff' : 'inherit';
    btn.title = mine ? 'Go back to rotating through all windows' : `Hold the receiver on ${what} until released`;
    note.textContent = mine ? '' : state.focus ? `now held on ${state.label}` : '';
  }
  async function refresh() {
    try { state = await (await fetch('/api/voice/focus')).json(); render(); } catch (e) { /* rxapp restarting */ }
  }
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await fetch('/api/voice/focus', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({focus: state.focus === want ? null : want})});
      state = await r.json();
    } catch (e) { /* ignore */ }
    btn.disabled = false;
    render();
  });
  async function ensure(isHere) {
    const here = isHere || ((f) => f === want);
    try { state = await (await fetch('/api/voice/focus')).json(); } catch (e) { return; }
    if (here(state.focus)) { render(); return; }
    try {
      const r = await fetch('/api/voice/focus', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                  body: JSON.stringify({focus: want})});
      if (r.ok) state = await r.json();
    } catch (e) { /* rxapp restarting: the next refresh shows the state */ }
    render();
  }
  window.receiverFocus = {ensure, want};
  refresh(); setInterval(refresh, 3000);
})();
