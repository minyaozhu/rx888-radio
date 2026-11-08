// SPDX-License-Identifier: GPL-3.0-or-later
// The FT8 / FT4 page's world map (d3, Natural Earth projection, 110 m countries), shared by WSPR and CW:
// stations as dots coloured by band (hollow = placed by call prefix, not a grid), great-circle lines from
// home, rings on stations on the air now, a home star; scroll to zoom, drag to pan, double-click to reset.
// Needs d3 and topojson-client, an <svg id="..."> and the page's --land / --land-edge / --water / --grat /
// --home / --ink / --panel colours.
'use strict';

const BAND_ORDER = ['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m', '12m', '10m', '6m'];
const BAND_COLORS = ['#8C564B', '#D62728', '#FF7F0E', '#BCBD22', '#2CA02C', '#17BECF', '#1F77B4', '#9467BD', '#E377C2', '#7F7F7F', '#4C4CA8'];
const bandColor = (b) => BAND_COLORS[Math.max(0, BAND_ORDER.indexOf(b))];

(function () {
  const st = document.createElement('style');
  st.textContent = `
.wmap{padding:8px;position:relative}
.wmap svg{width:100%;height:auto;display:block;border-radius:4px;background:var(--water);touch-action:none;cursor:grab}
.wmap .zoomhint{position:absolute;right:16px;bottom:14px;font:500 11px "IBM Plex Mono",monospace;color:var(--muted);background:var(--panel);padding:2px 6px;border-radius:3px;opacity:.85}
.wmap .land{fill:var(--land);stroke:var(--land-edge);stroke-width:.5;vector-effect:non-scaling-stroke}
.wmap .grat{fill:none;stroke:var(--grat);stroke-width:.5;vector-effect:non-scaling-stroke}
.wmap .arc{fill:none;vector-effect:non-scaling-stroke;stroke-linecap:round}
.wmap .stn{cursor:pointer;vector-effect:non-scaling-stroke}
.wmap .stn.approx{fill-opacity:0!important;stroke-width:1.5}
.wmap .stn.sel{stroke:var(--ink)!important;stroke-width:2.5}
.wmap .ring{fill:none;stroke-width:1.4;vector-effect:non-scaling-stroke}
.wmap .homestar{fill:var(--home);stroke:var(--panel);stroke-width:1;vector-effect:non-scaling-stroke}
.wmap .maptag{font:600 11px "IBM Plex Mono",monospace;fill:var(--ink);paint-order:stroke;stroke:var(--panel);stroke-width:3px;stroke-linejoin:round;pointer-events:none}
.wlegend{display:flex;flex-wrap:wrap;gap:6px 14px;font:600 13px "Barlow Condensed",sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);padding:2px 8px 6px}
.wlegend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.wlegend i.hollow{background:none!important;border:1.5px solid var(--muted);box-sizing:border-box}`;
  document.head.appendChild(st);
})();

// Stations placed by prefix get a small stable offset so a country's stations don't stack exactly
function callJitter(call) {
  let h = 2166136261;
  for (const c of call) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return [((h & 0xFF) / 255 - 0.5) * 2.4, (((h >> 8) & 0xFF) / 255 - 0.5) * 3.2];
}
function kmBetween([la1, lo1], [la2, lo2]) {
  const r = Math.PI / 180, a = Math.sin((la2 - la1) * r / 2) ** 2 +
    Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin((lo2 - lo1) * r / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.sqrt(a));
}

// worldMap('#map', {onPick(id)}) -> {set({stations, lines, rings, labels, home, homeLabel}), focus([lat, lon])}
//   stations: [{id, ll: [lat, lon], band, approx, op, n, title, sel}]
//   lines:    [{id, from: [lat, lon], to: [lat, lon], band, w, op}]       (great circles)
//   rings:    [[lat, lon], ...]                                            (on the air now)
//   labels:   [{ll, text}]
function worldMap(sel, opts = {}) {
  const svg = d3.select(sel).attr('viewBox', '0 0 960 500');
  const M = {k: 1, d: {stations: [], lines: [], rings: [], labels: [], home: null, homeLabel: 'Home'}};
  M.proj = d3.geoNaturalEarth1().fitExtent([[6, 6], [954, 494]], {type: 'Sphere'});
  M.path = d3.geoPath(M.proj);
  const root = svg.append('g');
  root.append('path').datum({type: 'Sphere'}).attr('d', M.path).attr('fill', 'var(--water)');
  root.append('path').datum(d3.geoGraticule10()).attr('class', 'grat').attr('d', M.path);
  const landG = root.append('g'), arcs = root.append('g'), rings = root.append('g'), stns = root.append('g'),
    home = root.append('g'), tags = root.append('g');
  const zoom = d3.zoom().scaleExtent([1, 14]).translateExtent([[0, 0], [960, 500]])
    .on('zoom', (e) => { M.k = e.transform.k; root.attr('transform', e.transform); render(); });
  svg.call(zoom).on('dblclick.zoom', null);
  svg.on('dblclick', () => svg.transition().duration(400).call(zoom.transform, d3.zoomIdentity));
  d3.json('https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json').then((world) => {
    landG.selectAll('path').data(topojson.feature(world, world.objects.countries).features)
      .join('path').attr('class', 'land').attr('d', M.path);
  }).catch((e) => console.warn('world map', e));
  const xy = (ll) => M.proj([ll[1], ll[0]]);

  function render() {
    const k = M.k, s = Math.sqrt(k), d = M.d;
    arcs.selectAll('path').data(d.lines, (l) => l.id).join('path').attr('class', 'arc')
      .attr('d', (l) => M.path({type: 'LineString', coordinates: [[l.from[1], l.from[0]], [l.to[1], l.to[0]]]}))
      .attr('stroke', (l) => bandColor(l.band)).attr('stroke-width', (l) => l.w || 1.6).attr('opacity', (l) => l.op ?? 0.7);
    const pts = d.stations.filter((p) => p.ll).sort((a, b) => (a.op ?? 1) - (b.op ?? 1));
    stns.selectAll('circle').data(pts, (p) => p.id).join('circle')
      .attr('class', (p) => `stn${p.approx ? ' approx' : ''}${p.sel ? ' sel' : ''}`)
      .attr('cx', (p) => xy(p.ll)[0]).attr('cy', (p) => xy(p.ll)[1])
      .attr('r', (p) => (3 + Math.min(3, Math.log2(1 + (p.n || 1)) * 0.8)) / s)
      .attr('fill', (p) => bandColor(p.band)).attr('stroke', (p) => bandColor(p.band)).attr('opacity', (p) => p.op ?? 1)
      .on('click', (e, p) => opts.onPick && opts.onPick(p.id))
      .selectAll('title').data((p) => [p]).join('title').text((p) => p.title || p.id);
    rings.selectAll('circle').data(d.rings).join('circle').attr('class', 'ring')
      .attr('cx', (ll) => xy(ll)[0]).attr('cy', (ll) => xy(ll)[1]).attr('r', 9 / s)
      .attr('stroke', 'var(--p-cq, #2E7B57)').attr('opacity', 0.85);
    const hs = d.home ? [d.home] : [];
    home.selectAll('path').data(hs).join('path').attr('class', 'homestar').attr('d', (ll) => {
      const [x, y] = xy(ll), r = 7 / s;
      return 'M' + d3.range(10).map((i) => {
        const a = Math.PI / 5 * i - Math.PI / 2, rr = i % 2 ? r * 0.45 : r;
        return [x + rr * Math.cos(a), y + rr * Math.sin(a)];
      }).join('L') + 'Z';
    }).selectAll('title').data([0]).join('title').text(d.homeLabel);
    tags.selectAll('text').data(d.labels.filter((l) => l.ll), (l) => l.text).join('text').attr('class', 'maptag')
      .attr('x', (l) => xy(l.ll)[0] + 6 / s).attr('y', (l) => xy(l.ll)[1] - 6 / s)
      .attr('font-size', 11 / s).attr('stroke-width', 3 / s).text((l) => l.text);
  }
  M.set = (d) => { Object.assign(M.d, d); render(); };
  M.focus = (ll) => {
    if (!ll) return;
    const [x, y] = xy(ll), k = Math.max(M.k, 3);
    svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity.translate(480 - x * k, 250 - y * k).scale(k));
  };
  return M;
}
