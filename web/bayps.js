// Bay Area public-safety channels and radio codes, for the public-safety page.
// Channel names: CHP San Jose from RadioReference forum reports; the rest are channels the receivers
// have heard but not identified (the page labels them by frequency and service band).
'use strict';

const PS_CHANNELS = {
  '42500000': 'CHP San Jose dispatch ("Ruby")',
  '42280000': 'CHP San Jose mobiles',
};

const PS_BANDS = [
  [30e6, 50e6, 'VHF low band (CHP, some counties)'],
  [150.8e6, 162e6, 'VHF high band'],
  [450e6, 470e6, 'UHF'],
];

function psLabel(hz) {
  const k = String(Math.round(hz / 100) * 100);
  if (PS_CHANNELS[k]) return PS_CHANNELS[k];
  const b = PS_BANDS.find(([lo, hi]) => hz >= lo && hz <= hi);
  return b ? (hz < 50e6 ? 'low band' : hz < 162e6 ? 'VHF-high' : 'UHF') : '';
}

// Common meanings; agencies differ, so treat these as a guide. CHP uses the 11-code series heavily.
const PS_CODES = {
  '10-4': 'acknowledged / OK', '10-7': 'out of service', '10-8': 'in service', '10-9': 'repeat',
  '10-15': 'prisoner in custody', '10-19': 'return to station', '10-20': 'location', '10-21': 'phone',
  '10-22': 'disregard', '10-23': 'arrived / stand by', '10-28': 'registration check', '10-29': 'check for wants',
  '10-33': 'emergency traffic', '10-35': 'confidential information', '10-97': 'arrived on scene',
  '10-98': 'assignment completed',
  '11-24': 'abandoned vehicle', '11-25': 'traffic hazard', '11-26': 'disabled vehicle', '11-27': 'driver license check',
  '11-28': 'registration check', '11-41': 'ambulance needed', '11-44': 'fatality', '11-48': 'provide transportation',
  '11-79': 'collision, ambulance responding', '11-80': 'collision, major injuries', '11-81': 'collision, minor injuries',
  '11-82': 'collision, property damage only', '11-83': 'collision, no details', '11-84': 'traffic control',
  '11-85': 'tow truck needed', '11-98': 'meet', '11-99': 'officer needs help (emergency)',
  'code 2': 'urgent, no lights and siren', 'code 3': 'emergency, lights and siren', 'code 4': 'no further help needed',
  'code 5': 'stakeout', 'code 6': 'out of the vehicle to investigate', 'code 7': 'meal break',
};

// Find codes in a transcript: "11-80", "1180", "11 80", "10-4", "code 3"
function psCodes(text) {
  const out = [];
  const t = (text || '').toLowerCase();
  for (const m of t.matchAll(/\b(1[01])[- ]?(\d{1,2})\b/g)) {
    const k = `${m[1]}-${+m[2]}`;
    if (PS_CODES[k]) out.push([m[0], k, PS_CODES[k]]);
  }
  for (const m of t.matchAll(/\bcode ([2-7])\b/g)) out.push([m[0], `code ${m[1]}`, PS_CODES[`code ${m[1]}`]]);
  return out;
}
