"""Unit tests for rxapp's pure logic (no radio needed)."""

import struct

import numpy as np
import pytest

from rxapp.addr import maddr
from rxapp.jobs import BANDS, Scanner
from rxapp.radio import estimate_snr
from rxapp.spectrum import Frame


@pytest.mark.parametrize("name, addr", [
    # Addresses radiod itself chose for these names (from its logs and pcmrecord)
    ("am.local", "239.57.202.156"),
    ("rx888-status.local", "239.210.144.91"),
    ("rx888-pcm.local", "239.105.116.5"),
])
def test_maddr_matches_radiod(name, addr):
    assert maddr(name) == addr


def test_maddr_avoids_control_block_aliases():
    for i in range(2000):
        a = [int(x) for x in maddr(f"name{i}.local").split(".")]
        assert a[0] == 239
        assert (a[1] & 0x7F, a[2]) != (0, 0)


def test_snr_estimate():
    # 10 kHz filter, noise density -140 dB/Hz -> noise -100 dB; power -60 dB -> ~40 dB SNR
    st = {"baseband_power": -60.0, "noise_density": -140.0, "low_edge": -5000.0, "high_edge": 5000.0}
    assert estimate_snr(st) == pytest.approx(40.0, abs=0.01)
    # pure noise reads as very low SNR, not an error
    st["baseband_power"] = -100.0
    assert estimate_snr(st) < -20
    assert estimate_snr({"baseband_power": None}) is None


def _fake_band(freqs, carriers, floor=-120.0):
    db = np.full(len(freqs), floor) + np.random.default_rng(0).normal(0, 1, len(freqs))
    for f, level in carriers:
        db[np.argmin(np.abs(freqs - f))] = level
    return db


def test_grid_scan_finds_carriers_on_channel_grid():
    band = BANDS["am"]
    freqs = 1.1e6 + (np.arange(2048) - 1024) * 1000.0
    db = _fake_band(freqs, [(860e3, -60), (1220e3, -70), (680.3e3, -80)])  # 680 slightly off-grid
    hits = Scanner._grid_hits(band, freqs, db, floor=-120.0, min_snr=8)
    assert [f for f, _ in hits] == [860e3, 1220e3, 680e3]
    assert hits[0][1] == pytest.approx(60, abs=0.1)


def test_peak_scan_merges_one_ssb_signal():
    freqs = 7.2e6 + (np.arange(2048) - 1024) * 100.0
    db = _fake_band(freqs, [])
    i = np.argmin(np.abs(freqs - 7.1505e6))
    db[i - 10:i + 10] = -90           # a 2 kHz-wide SSB voice signal
    db[i] = -85
    inband = (freqs >= 7.125e6) & (freqs <= 7.3e6)
    hits = Scanner._peak_hits(freqs, db, inband, floor=-120.0, min_snr=8, bin_hz=100)
    assert len(hits) == 1
    assert hits[0][0] == pytest.approx(7.1505e6, abs=200)


def test_frame_wire_format():
    f = Frame(center_hz=1.1e6, bin_hz=1000.0, db=np.array([-100, -90, -80], np.float32), t=0)
    b = f.to_bytes()
    center, bin_hz, n = struct.unpack("<dfI", b[:16])
    assert (center, bin_hz, n) == (1.1e6, 1000.0, 3)
    assert np.frombuffer(b[16:], "<f4").tolist() == [-100, -90, -80]
    assert f.freqs.tolist() == [1.099e6, 1.1e6, 1.101e6]


# ---- VHF mapping, span limits ---------------------------------------------
from types import SimpleNamespace

from rxapp.config import RUNTIME
from rxapp.spectrum import Spectrum
from rxapp.vhf import WINDOW_HZ, Vhf


def test_vhf_if_mapping_is_inverted_and_reversible():
    v = Vhf()
    v.center_hz, v.if_hz = 97.4e6, 4.57e6
    assert v.to_if(97.4e6) == 4.57e6            # the tuned frequency sits at the IF
    assert v.to_if(98.5e6) == pytest.approx(3.47e6)   # higher RF -> lower IF (LO above RF)
    assert v.to_rf(v.to_if(101.1e6)) == pytest.approx(101.1e6)
    assert v.radiod_mode("usb") == "lsb" and v.radiod_mode("wfm") == "wfm"
    assert v.in_window(97.4e6 + WINDOW_HZ) and not v.in_window(97.4e6 + WINDOW_HZ + 1)


def _spectrum(band, center=None):
    radio = SimpleNamespace(band=band, vhf=SimpleNamespace(center_hz=center, to_if=lambda f: f), on_tuner_move=[])
    return Spectrum(radio)


@pytest.mark.parametrize("rate", [64.8e6, 129.6e6])
def test_hf_span_never_exceeds_nyquist(rate, monkeypatch):
    # radiod returns half-empty frames when the span is wider than Nyquist
    monkeypatch.setattr(RUNTIME, "adc_rate_hz", rate)
    s = _spectrum("hf")
    s.bin_hz, s.center_hz = 16000.0, 16e6        # the old "full HF" request: 32.77 MHz
    s._clamp()
    assert s.span_hz <= rate / 2 + 1
    assert s.center_hz - s.span_hz / 2 >= -1 and s.center_hz + s.span_hz / 2 <= rate / 2 + 1


def test_vhf_live_view_stays_in_tuner_window():
    s = _spectrum("vhf", center=100e6)
    s.bin_hz, s.center_hz = 4000.0, 110e6        # 8.2 MHz: live, so it must stay on the tuner
    s._clamp()
    assert not s.wants_sweep
    assert abs(s.center_hz - 100e6) + s.span_hz / 2 <= WINDOW_HZ + 1


def test_vhf_wide_view_becomes_a_sweep(monkeypatch):
    monkeypatch.setattr(RUNTIME, "adc_rate_hz", 129.6e6)
    s = _spectrum("vhf", center=100e6)
    s.bin_hz, s.center_hz = 40000.0, 90e6          # asks for 82 MHz: capped at the 64.8 MHz coverage
    s._clamp()
    assert s.wants_sweep and s.span_hz == pytest.approx(64.8e6)
    s.sweeping = True                              # leaving a sweep: the view picks the tuner position
    s.bin_hz, s.center_hz = 4000.0, 87.5e6
    s._clamp(follow_tuner=False)
    assert s.center_hz == 87.5e6 and not s.wants_sweep


# ---- FT8 ------------------------------------------------------------------
from rxapp import ft8


def test_ft8_parse_and_parties():
    out = ("000000 +13.5 +0.08 1497 ~  N2SAB W5WCA DN70\n"
           "000000  -4.0 -0.30  812 ~  CQ DX JA1XYZ PM95\n"
           "000000  -9.1 +1.10 2210 ~  CQ K1ABC FN42\n"
           "000000  -9.1 +1.10 2210 ~  CQ K1ABC FN42\n"          # duplicate line
           "decoded 3 messages\n")
    rows = ft8.parse(out)
    assert [r["msg"] for r in rows] == ["N2SAB W5WCA DN70", "CQ DX JA1XYZ PM95", "CQ K1ABC FN42"]
    assert (rows[0]["score"], rows[0]["dt"], rows[0]["hz"]) == (13.5, 0.08, 1497)
    assert (rows[0]["to_call"], rows[0]["from_call"], rows[0]["extra"]) == ("N2SAB", "W5WCA", "DN70")
    assert (rows[1]["to_call"], rows[1]["from_call"], rows[1]["extra"]) == ("CQ", "JA1XYZ", "PM95")
    assert ft8.grid_of("PM95") == "PM95" and ft8.grid_of("RR73") is None and ft8.grid_of("-12") is None


def _packets(feed, t_start, seconds, delay_jitter=0.0, ts0=1000, monkeypatch=None, ramp=True):
    """Feed 20 ms packets whose first sample was taken at wall time t_start."""
    rng = np.random.default_rng(1)
    n = ft8.RATE // 50
    for k in range(int(seconds * 50)):
        t_end = t_start + (k + 1) * n / ft8.RATE
        arrival = t_end + 0.05 + abs(rng.normal(0, delay_jitter))
        monkeypatch.setattr(ft8.time, "time", lambda a=arrival: a)
        a = (np.arange(k * n, (k + 1) * n) % 30000).astype("<i2") if ramp else np.zeros(n, "<i2")
        feed.add(a.tobytes(), (ts0 + k * n) & 0xFFFFFFFF)


def test_ft8_slot_cut_uses_rtp_time_not_arrival(monkeypatch):
    feed = ft8.Feed("40m", 7.074e6)
    t_start = 1_800_000_000.0                                  # a slot boundary (divisible by 15)
    _packets(feed, t_start - 2, 20, delay_jitter=0.2, monkeypatch=monkeypatch)
    a = feed.cut(t_start, 15.0)
    assert a is not None and len(a) == 15 * ft8.RATE
    # The slot starts at the sample taken at t_start: 2 s into the stream. The simulated network delay
    # is 50 ms but the cut assumes LATENCY_SEC, so it lands (LATENCY_SEC - 0.05) s later than that.
    first = int(a[0])
    assert abs(first - (2 + ft8.LATENCY_SEC - 0.05) * ft8.RATE) <= 0.01 * ft8.RATE


def test_ft8_slot_cut_handles_timestamp_wrap_and_gaps(monkeypatch):
    feed = ft8.Feed("20m", 14.074e6)
    t_start = 1_800_000_015.0
    _packets(feed, t_start - 1, 17, ts0=0xFFFFFFFF - ft8.RATE * 3, monkeypatch=monkeypatch)
    assert feed.ts_hi == 1 << 32
    assert feed.cut(t_start, 15.0) is not None
    assert feed.cut(t_start + 30, 15.0) is None                # no audio for that slot


def test_ft8_conversations_group_pairs_and_cq():
    rows = [dict(t=t, band="40m", score=0, hz=1000, msg=m, **ft8.parties(m)) for t, m in [
        (0, "CQ W5WCA DN70"), (15, "W5WCA N2SAB FN20"), (30, "N2SAB W5WCA -08"),
        (45, "W5WCA N2SAB R-10"), (60, "N2SAB W5WCA RR73"), (60, "CQ K1ABC FN42")]]
    d = ft8.conversations(rows)
    (c,) = d["conversations"]
    assert (c["a"], c["b"], len(c["messages"]), c["complete"]) == ("N2SAB", "W5WCA", 4, True)
    assert {q["call"]: q["grid"] for q in d["cq"]} == {"W5WCA": "DN70", "K1ABC": "FN42"}


# ---- SSTV -----------------------------------------------------------------
import asyncio

from rxapp import sstvmon


def _sstv_audio(seconds_before=30, seconds_after=40):
    import sstv
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (320, 256), "navy")
    ImageDraw.Draw(im).rectangle([60, 60, 260, 196], fill="orange")
    tx = sstv.encode(im, sstv.Mode.MARTIN_1, sample_rate=sstvmon.RATE).astype(float) * 0.3
    rng = np.random.default_rng(0)
    pad = lambda s: rng.normal(0, 600, int(s * sstvmon.RATE))
    a = np.concatenate([pad(seconds_before), tx + rng.normal(0, 600, len(tx)), pad(seconds_after)])
    return np.clip(a, -32767, 32767).astype("<i2")


def test_sstv_detector_fires_on_sstv_not_on_noise():
    a = _sstv_audio().astype(float) / 32768
    r = sstvmon.RATE
    assert sstvmon.looks_like_sstv(sstvmon.analyse(a[60 * r:80 * r]))          # mid-picture
    assert not sstvmon.looks_like_sstv(sstvmon.analyse(a[:20 * r]))             # noise only


def test_sstv_monitor_captures_and_decodes_a_picture(tmp_path):
    saved = []
    store = SimpleNamespace(add_sstv=lambda **r: saved.append(r), event=lambda *a, **k: None)
    mon = sstvmon.SstvMonitor.__new__(sstvmon.SstvMonitor)
    mon.store, mon.dir = store, tmp_path
    w = sstvmon.Watch("20m", 14.230e6, "usb")
    audio = _sstv_audio()
    step = int(sstvmon.CHECK_EVERY * sstvmon.RATE)

    async def run():
        t = 1_800_000_000.0
        for i in range(0, len(audio), step):
            w.add(audio[i:i + step].tobytes())
            t += sstvmon.CHECK_EVERY
            await mon.check(w, t)
    asyncio.run(run())
    assert w.triggers == 1 and w.capture is None          # one capture, finished when the signal ended
    assert len(saved) == 1 and saved[0]["mode"] == "MARTIN_1" and saved[0]["complete"]
    assert (tmp_path / saved[0]["png"]).exists() and (tmp_path / saved[0]["wav"]).exists()


# ---- VHF tuner survives the RX-888 re-enumerating ---------------------------
from rxapp import vhf as vhfmod


class _FakeRx:
    """The demo driver's RX888: the first handle is dead (the radio re-enumerated), later ones work."""
    made = 0

    def __init__(self):
        type(self).made += 1
        self.dead = type(self).made == 1
        self.dev = None
        self.if_hz = 4.57e6
        self.freq = None

    def _io(self):
        if self.dead:
            raise SystemExit("RX888 on USB but firmware not answering TESTFX3 ([Errno 19] No such device)")

    def check_alive(self): self._io()
    def enter_vhf(self, *a): self._io()
    def clkb_on(self, *a): self._io()
    def r828d_init(self): self._io()
    def set_lna_agc(self, *a): self._io()
    def set_mixer_agc(self, *a): self._io()
    def set_vga_gain(self, *a): self._io()
    def standby(self, *a): self._io()

    def r828d_set_freq(self, f):
        self._io()
        self.freq = f
        return True


def test_vhf_tuner_reopens_a_dead_usb_handle(monkeypatch):
    monkeypatch.setattr(vhfmod.rx888_vhf, "RX888", _FakeRx)
    _FakeRx.made = 0
    v = vhfmod.Vhf()
    v._enable(135.7e6)                                   # first handle dead -> reopened, no SystemExit
    assert v.active and v.center_hz == 135.7e6 and _FakeRx.made == 2
    v._rx.dead = True                                    # radio drops again while tuned...
    v._retune(120.6e6)                                   # ...a retune reopens and re-programs at the NEW frequency
    assert v.center_hz == 120.6e6 and v._rx.freq == int(120.6e6) and _FakeRx.made == 3


def test_vhf_tuner_gives_up_with_an_error_not_an_exit(monkeypatch):
    class _Gone(_FakeRx):
        def __init__(self):
            super().__init__()
            self.dead = True
    monkeypatch.setattr(vhfmod.rx888_vhf, "RX888", _Gone)
    v = vhfmod.Vhf()
    with pytest.raises(RuntimeError):
        v._enable(135.7e6)


# ---- ADS-B demodulator --------------------------------------------------------
from rxapp import modes


def _ppm(hexmsg, rng, amp=600.0, noise=40.0):
    """A Mode S frame as 2 MS/s magnitude-like IQ: preamble pulses at 0, 1, 3.5, 4.5 us, then PPM bits."""
    bits = np.unpackbits(np.frombuffer(bytes.fromhex(hexmsg), np.uint8))
    m = np.zeros(modes.PRE + 2 * len(bits))
    m[[0, 2, 7, 9]] = amp
    for k, b in enumerate(bits):
        m[modes.PRE + 2 * k + (0 if b else 1)] = amp
    pad = np.zeros(400)
    x = np.concatenate([pad, m, pad, m * 0.5, pad])                     # the same frame twice, weaker the 2nd time
    return x + rng.normal(0, noise, len(x)) + 1j * rng.normal(0, noise, len(x))


def test_adsb_demodulator_recovers_frames():
    import pyModeS as pms
    msg = "8D4840D6202CC371C32CE0576098"                                 # KLM1023 identification (DF17)
    found = modes.demodulate(_ppm(msg, np.random.default_rng(3)))
    assert [h for _, h in found] == [msg, msg]
    d = pms.decode(found[0][1])
    assert d["crc_valid"] and d["callsign"] == "KLM1023"
    noise = np.random.default_rng(4).normal(0, 40, 200000) + 1j * np.random.default_rng(5).normal(0, 40, 200000)
    # noise yields some candidate frames; none survives the CRC check
    assert not any(pms.decode(h).get("crc_valid") for _, h in modes.demodulate(noise))


# ---- ADS-B feed (adsb.fi) ---------------------------------------------------------
from rxapp.adsb import AdsbFeed, FORGET_SEC


def test_adsb_feed_tracks_and_forgets():
    f = AdsbFeed()
    t0 = 1_800_000_000.0
    f.ingest({"ac": [{"hex": "a1b2c3", "flight": "AAL2105 ", "t": "A321", "lat": 37.5, "lon": -122.1,
                      "alt_baro": 3200, "gs": 210, "track": 284, "seen_pos": 1.0},
                     {"hex": "c0ffee", "flight": "UAL499", "lat": 37.62, "lon": -122.38, "alt_baro": "ground"},
                     {"hex": "nopos"}]}, now=t0)
    f.ingest({"ac": [{"hex": "a1b2c3", "flight": "AAL2105", "lat": 37.51, "lon": -122.14, "alt_baro": 3000,
                      "seen_pos": 0.5}]}, now=t0 + 5)
    a = f.aircraft["a1b2c3"]
    assert a["flight"] == "AAL2105" and a["alt"] == 3000 and len(f.tracks["a1b2c3"]) == 2
    assert f.tracks["a1b2c3"][0][0] == t0 - 1.0                 # positions are timed by when they were received
    assert f.aircraft["c0ffee"]["ground"] and f.aircraft["c0ffee"]["alt"] == 0
    assert "nopos" not in f.aircraft
    snap = f.snapshot(since=t0)                                  # only newer track points
    assert [len(x["trail"]) for x in snap["aircraft"] if x["hex"] == "a1b2c3"] == [1]
    f.ingest({"ac": []}, now=t0 + FORGET_SEC + 10)
    assert not f.aircraft and not f.tracks
