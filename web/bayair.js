// Bay Area airports and their VHF frequencies (AirNav / FAA Chart Supplement, October 2026).
// Positions are the airport reference points.
'use strict';

const BAY_AIRPORTS = [
  {id: 'SFO', name: 'San Francisco Intl', lat: 37.6188, lon: -122.3754,
   f: {'120.5': 'Tower', '121.8': 'Ground', '124.25': 'Ground', '118.85': 'ATIS', '118.2': 'Clearance'}},
  {id: 'OAK', name: 'Oakland Intl', lat: 37.7213, lon: -122.2212,
   f: {'118.3': 'Tower', '127.2': 'Tower (12/30)', '124.9': 'Tower', '121.75': 'Ground', '121.9': 'Ground (12/30)',
       '133.775': 'ATIS', '121.1': 'Clearance'}},
  {id: 'SJC', name: 'San José Intl', lat: 37.3630, lon: -121.9286,
   f: {'124.0': 'Tower', '121.7': 'Ground', '126.95': 'ATIS', '118.0': 'Clearance'}},
  {id: 'PAO', name: 'Palo Alto', lat: 37.4611, lon: -122.1150, f: {'118.6': 'Tower', '125.0': 'Ground', '135.275': 'ATIS'}},
  {id: 'SQL', name: 'San Carlos', lat: 37.5119, lon: -122.2495, f: {'119.0': 'Tower', '118.025': 'Ground', '125.9': 'ATIS'}},
  {id: 'NUQ', name: 'Moffett Field', lat: 37.4161, lon: -122.0491, f: {'119.55': 'Tower', '121.85': 'Ground', '124.175': 'ATIS'}},
  {id: 'HWD', name: 'Hayward Executive', lat: 37.6589, lon: -122.1217,
   f: {'120.2': 'Tower', '118.9': 'Tower', '121.4': 'Ground', '126.7': 'ATIS', '128.05': 'Clearance'}},
  {id: 'RHV', name: 'Reid-Hillview', lat: 37.3329, lon: -121.8198, f: {'119.8': 'Tower', '126.1': 'Tower (13L/31R)', '121.65': 'Ground', '125.2': 'ATIS'}},
  {id: 'LVK', name: 'Livermore', lat: 37.6934, lon: -121.8204, f: {'118.1': 'Tower', '121.6': 'Ground', '119.65': 'ATIS'}},
  {id: 'CCR', name: 'Concord Buchanan', lat: 37.9897, lon: -122.0569, f: {'119.7': 'Tower', '123.9': 'Tower', '121.9': 'Ground', '124.7': 'ATIS'}},
  {id: 'APC', name: 'Napa County', lat: 38.2132, lon: -122.2807, f: {'118.7': 'Tower', '121.7': 'Ground', '124.05': 'ATIS'}},
  {id: 'HAF', name: 'Half Moon Bay', lat: 37.5134, lon: -122.5012, f: {'122.8': 'CTAF', '127.275': 'AWOS'}},
];

// Radar controllers: one frequency serves many airports, so these aren't pinned to one
const BAY_SECTORS = {
  'NorCal Approach': ['128.325', '134.5', '133.95', '135.1', '125.35', '135.65', '120.1', '126.475', '124.4', '123.85', '135.2', '127.0'],
  'NorCal Departure': ['120.9', '121.3'],
  'Travis Approach': ['119.9'],
  'Oakland Center': ['127.8'],
};

// Frequency (Hz) -> {label, airport?, role}. Several airports share ground frequencies (121.7, 121.9);
// the first listed wins, and the label says so.
const BAY_FREQ = new Map();
for (const a of BAY_AIRPORTS) {
  for (const [f, role] of Object.entries(a.f)) {
    const hz = Math.round(parseFloat(f) * 1e6);
    const prev = BAY_FREQ.get(hz);
    if (prev) { prev.shared = (prev.shared || []).concat(a.id); prev.label += '/' + a.id; continue; }
    BAY_FREQ.set(hz, {label: `${a.id} ${role}`, airport: a, role});
  }
}
for (const [name, fs] of Object.entries(BAY_SECTORS)) {
  for (const f of fs) {
    const hz = Math.round(parseFloat(f) * 1e6);
    if (!BAY_FREQ.has(hz)) BAY_FREQ.set(hz, {label: name, sector: name, role: name.split(' ')[1]});
  }
}
const freqInfo = (hz) => BAY_FREQ.get(Math.round(hz / 1000) * 1000) || null;
