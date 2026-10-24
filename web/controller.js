// SPDX-License-Identifier: GPL-3.0-or-later
// Controller support for the tab shell: keys (from the keyboard, the Ulanzi Dial or any macro pad) and,
// optionally, the scroll wheel become radio actions on every tab. The bindings are learned in the
// 🎛 panel (press the button you want, it is recorded) and kept by rxapp in rxapp_data/controller.json.
'use strict';

const CTRL_ACTIONS = [
  ['tune_up', 'Tune up one step'], ['tune_down', 'Tune down one step'],
  ['tune_up_fast', 'Tune up 10 steps'], ['tune_down_fast', 'Tune down 10 steps'],
  ['zoom_in', 'Zoom the panel in (on the tuned frequency)'], ['zoom_out', 'Zoom the panel out'],
  ['step_up', 'Larger tuning step'], ['step_down', 'Smaller tuning step'], ['step_cycle', 'Next tuning step (wraps around)'],
  ['vol_up', 'Volume up'], ['vol_down', 'Volume down'], ['listen', 'Listen / stop'],
  ['mode_next', 'Next mode'], ['mode_prev', 'Previous mode'],
  ['chan_next', 'Next channel'], ['chan_prev', 'Previous channel'],
  ['band', 'Switch HF / VHF'], ['record', 'Record / stop'],
  ['tab_next', 'Next tab'], ['tab_prev', 'Previous tab'], ['tab_radio', 'Go to the Radio tab'],
];
const CTRL_DEFAULTS = {
  ArrowRight: 'tune_up', ArrowLeft: 'tune_down', 'Alt+ArrowRight': 'tune_up_fast', 'Alt+ArrowLeft': 'tune_down_fast',
  'Shift+ArrowRight': 'zoom_in', 'Shift+ArrowLeft': 'zoom_out',     // Shift + the dial (sending ← / →) zooms
  BracketRight: 'tune_up', BracketLeft: 'tune_down', 'Shift+BracketRight': 'tune_up_fast', 'Shift+BracketLeft': 'tune_down_fast',
  Slash: 'step_cycle', Period: 'step_up', Comma: 'step_down', Equal: 'vol_up', Minus: 'vol_down', KeyL: 'listen',
  KeyM: 'mode_next', 'Shift+KeyM': 'mode_prev',
  KeyN: 'chan_next', 'Shift+KeyN': 'chan_prev', KeyB: 'band', PageDown: 'tab_next', PageUp: 'tab_prev',
};
const ctrl = {bindings: {...CTRL_DEFAULTS}, learning: null, last: ''};
window.rxCtrl = ctrl;                   // pages in the frames check for it

// "Ctrl+Shift+KeyK" from a key event (physical key code, so it doesn't depend on the keyboard layout)
function comboOf(e) {
  const mods = [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && 'Shift', e.metaKey && 'Cmd'].filter(Boolean);
  return [...mods, e.code || e.key].join('+');
}
function wheelCombo(e) {
  const mods = [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && 'Shift', e.metaKey && 'Cmd'].filter(Boolean);
  const dy = e.deltaY || e.deltaX;
  return [...mods, dy < 0 ? 'WheelUp' : 'WheelDown'].join('+');
}
const KEYNAME = {BracketLeft: '[', BracketRight: ']', Period: '.', Comma: ',', Equal: '=', Minus: '−', Slash: '/', Backslash: '\\',
  Semicolon: ';', Quote: "'", Backquote: '`', Space: 'Space', ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→',
  WheelUp: 'wheel ↑', WheelDown: 'wheel ↓', PageUp: 'Page Up', PageDown: 'Page Down'};
const pretty = (c) => c.split('+').map((p) => KEYNAME[p] || p.replace(/^Key/, '').replace(/^Digit/, '').replace(/^Numpad/, 'Num ')).join(' + ');

function typing(t) {
  if (!t || !t.tagName) return false;
  if (t.isContentEditable || t.tagName === 'TEXTAREA') return true;
  return t.tagName === 'INPUT' && !['range', 'checkbox', 'radio', 'button', 'submit'].includes(t.type);
}

// ---- running an action
function radioFrame() { return ensureFrame('radio'); }
async function runAction(action) {
  if (action === 'tab_next' || action === 'tab_prev' || action === 'tab_radio') {
    const i = TABS.findIndex((t) => t.id === current());
    const t = action === 'tab_radio' ? TABS[0] : TABS[(i + (action === 'tab_next' ? 1 : TABS.length - 1)) % TABS.length];
    location.hash = t.id; hud(t.label); return;
  }
  const f = radioFrame();
  for (let k = 0; k < 40 && !(f.contentWindow && f.contentWindow.rxControl); k++) await new Promise((r) => setTimeout(r, 150));
  const fn = f.contentWindow && f.contentWindow.rxControl;
  if (!fn) return hud('Radio page not ready');
  try { hud(fn(action) || action); }
  catch (e) { hud('error: ' + e.message); }
}

let hudTimer = null;
function hud(text) {
  const el = document.getElementById('ctrlHud');
  el.textContent = text; el.classList.add('on');
  clearTimeout(hudTimer); hudTimer = setTimeout(() => el.classList.remove('on'), 1400);
}

// ---- input from the shell and from every frame (same origin, so we can listen inside them)
function onKey(e) {
  if (['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
  const combo = comboOf(e);
  ctrl.last = combo; showLast();
  if (ctrl.learning) { e.preventDefault(); e.stopImmediatePropagation(); learn(combo); return; }
  const action = ctrl.bindings[combo];
  if (!action) return;
  const plain = !e.ctrlKey && !e.altKey && !e.metaKey;
  if (plain && typing(e.target)) return;             // let a text box have its letters
  if (e.target && e.target.tagName === 'SELECT' && /^Arrow(Up|Down)/.test(e.code)) return;
  if (/^Arrow/.test(e.code) && e.target && e.target.blur && e.target !== e.target.ownerDocument.body) e.target.blur();  // a focused control mustn't keep the arrows
  e.preventDefault(); e.stopImmediatePropagation();
  runAction(action);
}
let wheelAcc = 0, wheelAt = 0;
function onWheel(e) {
  const combo = wheelCombo(e);
  if (ctrl.learning) { e.preventDefault(); e.stopImmediatePropagation(); learn(combo); return; }
  const action = ctrl.bindings[combo];
  if (!action) return;
  e.preventDefault(); e.stopImmediatePropagation();
  // one action per detent: trackpads send many small deltas, a dial a few large ones
  const now = performance.now();
  if (now - wheelAt > 250) wheelAcc = 0;
  wheelAt = now; wheelAcc += Math.abs(e.deltaY || e.deltaX);
  if (wheelAcc < 40 && e.deltaMode === 0) return;
  wheelAcc = 0; ctrl.last = combo; showLast(); runAction(action);
}
function attach(win) {
  win.addEventListener('keydown', onKey, true);
  win.addEventListener('wheel', onWheel, {capture: true, passive: false});
}
attach(window);
const hookFrame = (f) => { const go = () => { try { attach(f.contentWindow); } catch (e) { /* not same origin */ } }; f.addEventListener('load', go); };

// ---- the panel
function showLast() { const el = document.getElementById('ctrlLast'); if (el) el.textContent = ctrl.last ? pretty(ctrl.last) : '—'; }
function drawPanel() {
  const by = {};
  for (const [c, a] of Object.entries(ctrl.bindings)) (by[a] = by[a] || []).push(c);
  document.getElementById('ctrlRows').innerHTML = CTRL_ACTIONS.map(([a, label]) => `<tr${ctrl.learning === a ? ' class="learning"' : ''}>
    <td>${label}</td><td>${(by[a] || []).map((c) => `<span class="kbd" title="${c}">${pretty(c)}<button data-unbind="${c}" title="Remove">×</button></span>`).join(' ')
      || '<span class="none">—</span>'}${ctrl.learning === a ? ' <span class="wait">press a button or turn the knob…</span>' : ''}</td>
    <td><button data-learn="${a}">${ctrl.learning === a ? 'Cancel' : 'Assign'}</button></td></tr>`).join('');
  const wheelPlain = Object.keys(ctrl.bindings).some((c) => c === 'WheelUp' || c === 'WheelDown');
  document.getElementById('ctrlWarn').style.display = wheelPlain ? '' : 'none';
}
function learn(combo) {
  const a = ctrl.learning; ctrl.learning = null;
  for (const [c, x] of Object.entries(ctrl.bindings)) if (c === combo && x !== a) delete ctrl.bindings[c];   // a key does one thing
  ctrl.bindings[combo] = a;
  save(); drawPanel(); hud(`${pretty(combo)} → ${CTRL_ACTIONS.find((x) => x[0] === a)[1]}`);
}
async function save() {
  try { await fetch('/api/controller', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bindings: ctrl.bindings})}); }
  catch (e) { hud('could not save: ' + e.message); }
}
async function loadBindings() {
  try {
    const d = await (await fetch('/api/controller')).json();
    if (d.bindings) {
      ctrl.bindings = d.bindings;
    }
  } catch (e) { /* defaults */ }
}

document.getElementById('ctrlBtn').addEventListener('click', () => {
  const p = document.getElementById('ctrlPanel');
  p.hidden = !p.hidden; ctrl.learning = null; if (!p.hidden) drawPanel();
});
document.getElementById('ctrlClose').addEventListener('click', () => { document.getElementById('ctrlPanel').hidden = true; ctrl.learning = null; });
document.getElementById('ctrlReset').addEventListener('click', () => { ctrl.bindings = {...CTRL_DEFAULTS}; ctrl.learning = null; save(); drawPanel(); });
document.getElementById('ctrlClear').addEventListener('click', () => { if (confirm('Remove every binding?')) { ctrl.bindings = {}; save(); drawPanel(); } });
document.getElementById('ctrlRows').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.unbind) { delete ctrl.bindings[b.dataset.unbind]; save(); }
  if (b.dataset.learn) ctrl.learning = ctrl.learning === b.dataset.learn ? null : b.dataset.learn;
  b.blur(); drawPanel();
});
loadBindings();
