// SPDX-License-Identifier: GPL-3.0-or-later
// One page plays audio at a time. A page that starts playing calls audioFocus.claim(stop); every other rxapp
// page (tab frames and separate browser tabs alike: same origin, one BroadcastChannel) that is playing then
// runs its own stop. A page that stops by itself calls audioFocus.release().
'use strict';
(function () {
  const bc = 'BroadcastChannel' in window ? new BroadcastChannel('rxapp-audio') : null;
  const me = Math.random().toString(36).slice(2);
  let stopper = null;
  window.audioFocus = {
    claim(stop) {
      stopper = stop;
      if (bc) bc.postMessage({playing: me});
    },
    release() { stopper = null; },
  };
  if (bc) bc.onmessage = (e) => {
    if (e.data && e.data.playing && e.data.playing !== me && stopper) {
      const s = stopper; stopper = null;
      try { s(); } catch (err) { /* the page is going away */ }
    }
  };
})();
