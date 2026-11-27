// SPDX-License-Identifier: GPL-3.0-or-later
// Light / dark theme for the spectrum + waterfall windows (the Radio tab's panels, minispec.js's tiles). Dark by
// default; the choice is kept in localStorage (KEY) and shared by every tab (a change in one reaches
// the others through the storage event). specTheme.t() is the current palette; specTheme.onChange(fn) redraws.
// The window frames are styled from CSS variables (--spec-*) set on <html>.
'use strict';
(function () {
  function lut(stops) {
    const out = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const x = i / 255 * (stops.length - 1), k = Math.min(Math.floor(x), stops.length - 2), f = x - k;
      for (let c = 0; c < 3; c++) out[i * 3 + c] = stops[k][c] + (stops[k + 1][c] - stops[k][c]) * f;
    }
    return out;
  }
  const THEMES = {
    dark: {                                   // a soft charcoal slate rather than black
      name: 'dark', bg: '#1f2630', bgRGB: [31, 38, 48], bar: '#29323d', border: '#3b4652', text: '#d3dbe3', strong: '#eef2f6',
      muted: '#9aa7b4', live: '#5fd39a', grid: '#313b47', gridText: '#8995a2', trace: '#6cc4ff', old: '#6b7683',
      ink: ['#5b8cff', '#38d6f0', '#b3ea4f', '#fbd34a', '#f87171'],
      legendBg: 'rgba(31,38,48,0.88)', legend: '#ffd84a', legendGlow: 'rgba(255,216,74,0.45)',
      mark: '#e98fc0', play: '#5fd39a', playFill: 'rgba(95,211,154,0.22)', scanFill: 'rgba(255,184,76,0.16)', scan: '#ffb84c',
      window: 'rgba(108,201,154,0.08)',
      lut: lut([[31, 38, 48], [44, 52, 98], [92, 48, 128], [160, 56, 118], [214, 82, 76], [245, 133, 34], [250, 197, 52], [252, 250, 170]]),
    },
    light: {
      name: 'light', bg: '#ffffff', bgRGB: [255, 255, 255], bar: '#f2f4ee', border: '#cbd3d8', text: '#1d2833', strong: '#1d2833',
      muted: '#5a6774', live: '#2e7b57', grid: '#e4e9ed', gridText: '#7a8693', trace: '#1f5fbf', old: '#a3adb8',
      ink: ['#1d4ed8', '#0891b2', '#4d7c0f', '#b45309', '#dc2626'],
      legendBg: 'rgba(255,214,10,0.95)', legend: '#1d2833', legendGlow: 'rgba(0,0,0,0)',
      mark: '#a3316f', play: '#0f7a46', playFill: 'rgba(15,122,70,0.18)', scanFill: 'rgba(230,140,0,0.18)', scan: '#c26a00',
      window: 'rgba(46,123,87,0.08)',
      lut: lut([[255, 255, 255], [214, 226, 243], [150, 185, 230], [80, 130, 210], [60, 60, 170], [150, 40, 140], [225, 60, 60], [250, 160, 30]]),
    },
  };
  const KEY = 'spec.theme.v2';              // v2: the default became dark, so an old saved 'light' doesn't stick
  let cur = 'dark';
  try { if (localStorage.getItem(KEY) === 'light') cur = 'light'; } catch (e) { /* private window: dark */ }
  const subs = [];
  function apply() {
    const t = THEMES[cur], s = document.documentElement.style;
    s.setProperty('--spec-bg', t.bg); s.setProperty('--spec-bar', t.bar); s.setProperty('--spec-border', t.border);
    s.setProperty('--spec-text', t.text); s.setProperty('--spec-strong', t.strong); s.setProperty('--spec-muted', t.muted);
    s.setProperty('--spec-live', t.live);
    for (const f of subs) { try { f(t); } catch (e) { console.error(e); } }
  }
  function set(name) {
    if (!THEMES[name] || name === cur) return;
    cur = name;
    try { localStorage.setItem(KEY, name); } catch (e) { /* private window */ }
    apply();
  }
  window.addEventListener('storage', (e) => { if (e.key === KEY && THEMES[e.newValue] && e.newValue !== cur) { cur = e.newValue; apply(); } });
  // a small ☀ / ☾ button for a window's title bar
  function button() {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'spec-theme';
    b.style.cssText = 'font:12px/1 system-ui,sans-serif;padding:3px 7px;border-radius:4px;cursor:pointer;min-width:0;'
      + 'border:1px solid var(--spec-border);background:transparent;color:var(--spec-text)';
    const draw = () => { b.textContent = cur === 'light' ? '☾' : '☀'; b.title = cur === 'light' ? 'Dark spectrum windows' : 'Light spectrum windows'; };
    b.addEventListener('click', () => set(cur === 'light' ? 'dark' : 'light'));
    subs.push(draw); draw();
    return b;
  }
  window.specTheme = {t: () => THEMES[cur], set, onChange: (f) => subs.push(f), button};
  apply();
})();
