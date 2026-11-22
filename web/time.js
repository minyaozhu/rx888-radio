// SPDX-License-Identifier: GPL-3.0-or-later
// Pacific time (America/Los_Angeles, PST/PDT) beside the UTC times the HF pages show.
// ptTime(t, sec) -> '10:42:05 PDT'; utcPt(t, sec) -> '17:42:05 UTC · 10:42:05 PDT';
// utcPtCell(t) -> '17:42 · 10:42' (compact, for tables); eibiPt('0000-0300') -> '0000-0300 · 1600-1900 PST'.
// t is Unix seconds. Plain globals, like the other shared scripts.
'use strict';
const PT_FMT = {};
function ptParts(t, sec) {
  const k = sec ? 's' : 'm';
  PT_FMT[k] = PT_FMT[k] || new Intl.DateTimeFormat('en-US', {timeZone: 'America/Los_Angeles', hourCycle: 'h23',
    hour: '2-digit', minute: '2-digit', second: sec ? '2-digit' : undefined, timeZoneName: 'short'});
  const p = Object.fromEntries(PT_FMT[k].formatToParts(new Date(t * 1000)).map((x) => [x.type, x.value]));
  return {hm: p.hour + ':' + p.minute + (sec ? ':' + p.second : ''), tz: p.timeZoneName};
}
function ptTime(t, sec) { const p = ptParts(t, sec); return p.hm + ' ' + p.tz; }
function utcPt(t, sec) { return new Date(t * 1000).toISOString().slice(11, sec ? 19 : 16) + ' UTC · ' + ptTime(t, sec); }
function utcPtCell(t) { return new Date(t * 1000).toISOString().slice(11, 16) + ' · ' + ptParts(t).hm; }
// an EiBi schedule time 'HHMM-HHMM' (UTC, today) with its Pacific equivalent
function eibiPt(s) {
  const m = /^(\d{2})(\d{2})-(\d{2})(\d{2})$/.exec(String(s || '').trim());
  if (!m) return s;
  const day = Math.floor(Date.now() / 86400000) * 86400;
  const a = ptParts(day + m[1] * 3600 + m[2] * 60), b = ptParts(day + m[3] * 3600 + m[4] * 60);
  return `${s} · ${a.hm.replace(':', '')}-${b.hm.replace(':', '')} ${a.tz}`;
}
