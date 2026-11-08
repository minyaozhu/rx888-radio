// SPDX-License-Identifier: GPL-3.0-or-later
// postStream(url, body, onPartial): POST to one of rxapp's streaming endpoints (…/stream, NDJSON) and call
// onPartial with each partial result as it arrives (stations as a scan finds them); resolves to the final result.
'use strict';
async function postStream(url, body, onPartial) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = '', done, finished = false;
  const line = (s) => {
    if (!s.trim()) return;
    const m = JSON.parse(s);
    if (m.error) throw new Error(m.error);
    if ('partial' in m && onPartial) onPartial(m.partial);
    if ('done' in m) { done = m.done; finished = true; }
  };
  for (;;) {
    const {value, done: end} = await reader.read();
    if (end) break;
    buf += dec.decode(value, {stream: true});
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { line(buf.slice(0, i)); buf = buf.slice(i + 1); }
  }
  line(buf);
  if (!finished) throw new Error('the scan stopped before it finished');
  return done;
}
