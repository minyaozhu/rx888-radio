// SPDX-License-Identifier: GPL-3.0-or-later
// "Reset stats": a page shows and counts only what came after the reset: counts, timelines, transcripts, logs,
// maps and galleries alike. Nothing is deleted; the reset time is kept in rxapp (POST /api/stats/reset), so every
// browser sees the same thing, and "show all" undoes it.
// Include before the page's script: <script src="statsreset.js" data-page="ft8"></script>. The button goes at the
// end of the page's .facts row (or into #statsBox). The page reads statsReset.since() (unix s, 0 = no reset) and
// redraws on statsReset.onChange(fn).
'use strict';
(function () {
  const me = document.currentScript;
  const page = (me.dataset.param && new URLSearchParams(location.search).get(me.dataset.param)) || me.dataset.page;
  let since = 0;
  const subs = [];
  const box = document.createElement('span');
  box.className = 'statsreset';
  box.style.cssText = 'display:inline-flex;align-items:center;gap:8px;font:500 12px ui-monospace,Menlo,monospace';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.style.cssText = 'font:600 12px/1 system-ui,sans-serif;letter-spacing:.04em;padding:6px 10px;border-radius:4px;cursor:pointer;border:1px solid currentColor;background:transparent;color:inherit;min-width:0';
  btn.textContent = 'Reset stats';
  const note = document.createElement('span');
  note.style.opacity = '.75';
  const undo = document.createElement('a');
  undo.href = '#';
  undo.textContent = 'show all';
  undo.style.color = 'inherit';
  box.append(btn, note, undo);
  function render() {
    const d = since ? new Date(since * 1000) : null;
    note.textContent = d ? `showing since ${d.toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false})}` : '';
    undo.style.display = d ? '' : 'none';
    btn.title = 'Show and count only what is heard from now on: counts, timeline, transcript (nothing is deleted)';
  }
  function set(t) {
    t = +t || 0;
    if (t === since) return;
    since = t; render();
    for (const f of subs) { try { f(since); } catch (e) { console.error(e); } }
  }
  async function post(on) {
    btn.disabled = true;
    try {
      const r = await fetch('/api/stats/reset', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify({page, on})});
      if (r.ok) set((await r.json())[page]);
    } catch (e) { /* rxapp restarting */ }
    btn.disabled = false;
  }
  btn.addEventListener('click', () => post(true));
  undo.addEventListener('click', (e) => { e.preventDefault(); post(false); });
  async function refresh() {
    try { set((await (await fetch('/api/stats/reset')).json())[page]); } catch (e) { /* rxapp restarting */ }
  }
  function mount() {
    const host = document.getElementById('statsBox') || document.querySelector('.facts');
    if (host) host.append(box);
    render(); refresh(); setInterval(refresh, 15000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
  // keep(t): is a record at unix time t (s, or ms when > 1e12) counted?
  const keep = (t) => !since || (t > 1e12 ? t / 1000 : t) >= since;
  window.statsReset = {since: () => since, keep, onChange: (f) => subs.push(f), page};
})();
