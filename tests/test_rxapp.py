# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for rxapp's pure logic (no radio needed)."""

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from rxapp.addr import maddr
from rxapp.jobs import BANDS, Scanner
from rxapp.radio import Channel, agc_seed, estimate_snr
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


def _band_noise(n, rng, lo=300.0, hi=2700.0):
    """Receiver noise as it comes out of an SSB filter: flat over lo-hi, unit RMS."""
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sstvmon.RATE)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    return x / np.sqrt((x ** 2).mean())


def _weak_sstv(mode_name, snr_db, seed=1):
    import sstv
    rng = np.random.default_rng(seed)
    mode = getattr(sstv.Mode, mode_name)
    img = rng.integers(0, 255, (mode.image_height, mode.image_width, 3)).astype(np.uint8)
    s = sstv.encode(img, mode, sstvmon.RATE).astype(float)
    s /= np.sqrt((s ** 2).mean())
    r = sstvmon.RATE
    a = _band_noise(len(s) + 30 * r, rng)
    a[10 * r:10 * r + len(s)] += s * 10 ** (snr_db / 20)
    return a / np.abs(a).max()


def test_sstv_detector_catches_weak_sstv_but_not_a_voice_net():
    r = sstvmon.RATE
    for mode in ("MARTIN_1", "SCOTTIE_1", "PD_120", "ROBOT_36"):
        a = _weak_sstv(mode, 0)                                      # 0 dB SNR in the SSB bandwidth
        assert sstvmon.looks_like_sstv(sstvmon.analyse(a[20 * r:40 * r])), mode
    assert not sstvmon.looks_like_sstv(sstvmon.analyse(_band_noise(20 * r, np.random.default_rng(5))))
    # a voice net: syllables of a few harmonics below 1100 Hz, gaps every ~0.23 s, a steady 668 Hz whistle
    rng = np.random.default_rng(7)
    t = np.arange(20 * r) / r
    gate = (np.sin(2 * np.pi * t / 0.227) > -0.3).astype(float)
    voice = sum(np.sin(2 * np.pi * h * 180 * t + rng.uniform(0, 6)) / h for h in range(1, 6)) * gate
    a = voice + 0.6 * np.sin(2 * np.pi * 668 * t) + 0.3 * _band_noise(len(t), rng)
    r_ = sstvmon.analyse(a / np.abs(a).max())
    assert not sstvmon.looks_like_sstv(r_), r_


def test_sstv_decoder_band_limits_noisy_audio():
    """At 15 dB SNR the decoder finds nothing in the raw audio but decodes it band-limited to 1050-2350 Hz."""
    import sstv
    a = _weak_sstv("PD_120", 15) * 12000
    assert sstv.decode(a.astype(np.int16), sstvmon.RATE) == []
    assert len(sstvmon.decode_pictures(a)) == 1


def test_sstv_noise_tolerant_decoder_finds_weak_pictures_and_nothing_in_noise():
    """At 5 dB the package returns nothing; the fallback finds the mode from the line rate and every line."""
    import sstv
    from rxapp import sstvdec
    r = sstvmon.RATE
    for mode, name in (("MARTIN_1", "Martin M1"), ("PD_120", "PD120"), ("ROBOT_36", "Robot 36")):
        a = _weak_sstv(mode, 5) * 12000
        assert sstv.decode(a.astype(np.int16), r) == []
        pics = sstvmon.decode_pictures(a)
        assert len(pics) == 1 and pics[0].info["sstv_mode"] == name
        m = sstvdec.BY_NAME[name]
        assert pics[0].info["sstv_lines"] >= m.height - 2
    assert sstvdec.decode(_band_noise(120 * r, np.random.default_rng(11)), r) == []


def test_sstv_decoder_reads_a_picture_right():
    """A clean Martin M1 picture comes back pixel-close, in place (no row or column shift)."""
    import sstv
    from rxapp import sstvdec
    rng = np.random.default_rng(3)
    img = np.repeat(np.repeat(rng.integers(0, 256, (32, 40, 3)), 8, 0), 8, 1).astype(np.uint8)
    s = sstv.encode(img, sstv.Mode.MARTIN_1, sstvmon.RATE).astype(float)
    a = np.concatenate([np.zeros(5 * sstvmon.RATE), s, np.zeros(5 * sstvmon.RATE)])
    a += _band_noise(len(a), rng) * 0.01 * s.std()
    im = np.asarray(sstvdec.decode(a, sstvmon.RATE)[0]).astype(float)
    err = {sh: np.abs(im[max(0, -sh):256 - max(0, sh)] - img[max(0, sh):256 - max(0, -sh)]).mean() for sh in (-1, 0, 1)}
    assert min(err, key=err.get) == 0                  # rows in place
    assert err[0] < 14                                 # sharp 8-px block edges blur a little at 12 kHz


def _sstv_radio(parked=False, opened=0.0):
    return SimpleNamespace(channels={None: SimpleNamespace(parked=parked, opened=opened)})


def test_sstv_reopened_channel_settles_again(tmp_path):
    """Switching the input VHF -> HF re-creates the channels; their AGC settling must not trigger a capture."""
    mon = sstvmon.SstvMonitor.__new__(sstvmon.SstvMonitor)
    mon.store, mon.dir, mon.radio = None, tmp_path, _sstv_radio()
    w = sstvmon.Watch("20m", 14.230e6, "usb")
    w.add(np.zeros(int(60 * sstvmon.RATE), np.int16).tobytes())     # a minute of audio: settled
    w.capture = [np.zeros(10, np.int16)]                             # mid-capture
    mon.radio.channels[None].parked = True                           # input -> VHF
    asyncio.run(mon.check(w, 0.0))
    assert w.capture is None and w.total_n == 0
    mon.radio.channels[None].parked, mon.radio.channels[None].opened = False, 123.0   # back to HF: re-created
    w.add(_sstv_audio()[:int(20 * sstvmon.RATE)].tobytes())         # stale audio from just before the check
    asyncio.run(mon.check(w, 1.0))
    assert w.total_n == 0 and w.opened == 123.0                      # settle period starts over
    w.add(_sstv_audio()[60 * sstvmon.RATE:80 * sstvmon.RATE].tobytes())   # 20 s of real SSTV, within SETTLE
    asyncio.run(mon.check(w, 2.0))
    assert w.capture is None and w.triggers == 0


def test_sstv_monitor_captures_and_decodes_a_picture(tmp_path):
    saved = []
    store = SimpleNamespace(add_sstv=lambda **r: saved.append(r), event=lambda *a, **k: None)
    mon = sstvmon.SstvMonitor.__new__(sstvmon.SstvMonitor)
    mon.store, mon.dir = store, tmp_path
    mon.radio = _sstv_radio()
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


# ---- voice sessions: windows, digital detection, retention, FM channel finding ------------
import json as _json
import time
import os

import voice_session as vs


def test_parse_windows_old_and_new_forms():
    w = vs.parse_windows("approach:135.7,lowband:hf:42.6:safety,vhfhigh:vhf:155.0:safety")
    assert [(x.name, x.band, x.centre, x.profile.service) for x in w] == [
        ("approach", "vhf", 135.7e6, "air"), ("lowband", "hf", 42.6e6, "safety"), ("vhfhigh", "vhf", 155.0e6, "safety")]
    with pytest.raises(ValueError):
        vs.parse_windows("x:uhf:450")


def _speech_like(rate, seconds, rng):
    t = np.arange(int(rate * seconds)) / rate
    syll = (np.sin(2 * np.pi * 3.5 * t) > 0.1).astype(float)          # syllables: bursts and gaps
    f0 = 140 + 20 * np.sin(2 * np.pi * 0.7 * t)
    voice = sum(np.sin(2 * np.pi * k * np.cumsum(f0) / rate) / k for k in range(1, 15))
    return ((voice * syll + 0.02 * rng.normal(size=len(t))) * 8000).astype("<i2")


def _p25_like(rate, seconds, rng):
    from scipy.signal import lfilter, firwin
    sym = rng.choice([-3, -1, 1, 3], int(4800 * seconds))
    x = np.repeat(sym, rate // 4800 + 1)[: int(rate * seconds)].astype(float)
    x = lfilter(firwin(63, 2400, fs=rate), 1, x)                        # band-limited 4-level symbols
    return (x / np.abs(x).max() * 8000).astype("<i2")


def test_digital_detector_separates_p25_from_speech():
    rng = np.random.default_rng(7)
    assert vs.looks_digital(_p25_like(24000, 3, rng), 24000)
    assert not vs.looks_digital(_speech_like(24000, 3, rng), 24000)


def test_prune_drops_old_recordings_and_lines(tmp_path):
    now = 1_800_000_000.0
    d = tmp_path / "20260101-000000"
    d.mkdir()
    old, new = d / "old.wav", d / "new.wav"
    old.write_bytes(b"x"); new.write_bytes(b"x")
    os.utime(old, (now - 8 * 86400,) * 2); os.utime(new, (now - 3600,) * 2)
    (d / "transmissions.jsonl").write_text(_json.dumps({"t0": now - 8 * 86400}) + "\n" + _json.dumps({"t0": now - 60}) + "\n")
    assert vs.prune(tmp_path, 7, now) == 1
    assert not old.exists() and new.exists()
    assert [_json.loads(l)["t0"] for l in (d / "transmissions.jsonl").read_text().splitlines()] == [now - 60]
    fresh = tmp_path / "20260102-000000"            # a session that has just started: empty, but not old
    fresh.mkdir()
    vs.prune(tmp_path, 7, time.time())
    assert fresh.exists()


def test_fm_signal_on_fine_grid_is_one_channel(monkeypatch):
    """A voice FM carrier lights several 2.5 kHz grid points; it must open exactly one channel."""
    s = vs.Session.__new__(vs.Session)
    s.windows = [vs.Window("lowband", "hf", 42.6e6, vs.SAFETY)]
    s.w, s.preload, s.channels, s.spurs = 0, [42.5e6], {}, set()
    s.keyed_since, s.last_keyed, s.peak, s.history = {}, {}, {}, __import__("collections").defaultdict(__import__("collections").deque)
    s.pending = {}
    s.lo, s.hi = 39e6, 46.6e6
    s.grid = np.arange(42.45e6, 42.55e6 + 1, 2.5e3)
    s.bin_of = {f: i for i, f in enumerate(s.grid)}
    opened = []
    monkeypatch.setattr(s, "ensure_channel", lambda f: (opened.append(f), s.channels.setdefault(f, {})))
    monkeypatch.setattr(s, "finish", lambda *a: None)
    db = np.full(len(s.grid), -100.0)
    for off, lvl in ((-5e3, 18), (-2.5e3, 24), (0, 30), (2.5e3, 25), (5e3, 17)):       # FM voice around 42.5025
        db[int(np.argmin(np.abs(s.grid - (42.5025e6 + off))))] = -100 + lvl
    s.step(1000.0, db)
    assert opened == []                  # one frame is not enough: noise spikes last one frame
    s.step(1000.1, db)
    s.step(1000.2, db)
    assert opened == [42.5e6]            # three frames: snapped to the known channel, once
    assert s.keyed_since[42.5e6] == 1000.0   # and the transmission starts at the first frame


def test_busy_channel_is_not_mistaken_for_a_spur(monkeypatch):
    """A channel keyed 60% of the time for over a minute is busy, not a spur (which is keyed ~100%)."""
    s = vs.Session.__new__(vs.Session)
    s.windows = [vs.Window("lowband", "hf", 42.6e6, vs.SAFETY)]
    s.w, s.preload, s.spurs, s.pending = 0, [42.5e6], set(), {}
    s.channels = {42.5e6: {}}
    s.keyed_since, s.last_keyed, s.peak = {}, {}, {}
    s.history = __import__("collections").defaultdict(__import__("collections").deque)
    s.lo, s.hi = 39e6, 46.6e6
    s.grid = np.arange(42.45e6, 42.55e6 + 1, 2.5e3)
    s.bin_of = {f: i for i, f in enumerate(s.grid)}
    monkeypatch.setattr(s, "ensure_channel", lambda f: None)
    monkeypatch.setattr(s, "finish", lambda *a: None)
    monkeypatch.setattr(s, "drop_spur", lambda f: s.spurs.add(f))
    quiet = np.full(len(s.grid), -100.0)
    busy = quiet.copy(); busy[int(np.argmin(np.abs(s.grid - 42.5e6)))] = -70
    for i in range(900):                                  # 90 s at 10 frames/s: 6 s talking, 4 s quiet
        s.step(1000 + i / 10, busy if (i // 10) % 10 < 6 else quiet)
    assert 42.5e6 not in s.spurs


def test_sidebands_of_a_strong_fm_signal_are_ignored(monkeypatch):
    s = vs.Session.__new__(vs.Session)
    s.windows = [vs.Window("lowband", "hf", 42.6e6, vs.SAFETY)]
    s.w, s.preload, s.spurs, s.pending, s.channels = 0, [], set(), {}, {}
    s.keyed_since, s.last_keyed, s.peak = {}, {}, {}
    s.history = __import__("collections").defaultdict(__import__("collections").deque)
    s.lo, s.hi = 39e6, 46.6e6
    s.grid = np.arange(41.05e6, 41.15e6 + 1, 2.5e3)
    s.bin_of = {f: i for i, f in enumerate(s.grid)}
    opened = []
    monkeypatch.setattr(s, "ensure_channel", lambda f: (opened.append(f), s.channels.setdefault(f, {})))
    monkeypatch.setattr(s, "finish", lambda *a: None)
    db = np.full(len(s.grid), -100.0)
    idx = lambda f: int(np.argmin(np.abs(s.grid - f)))
    db[idx(41.1e6)] = -70                                   # the signal, +30 dB
    db[idx(41.08e6)] = db[idx(41.12e6)] = -84              # its sidebands, +16 dB
    for i in range(3):
        s.step(1000 + i / 10, db)
    assert opened == [41.1e6]


def test_focus_holds_the_rotation_on_one_kind_of_window(monkeypatch):
    s = vs.Session.__new__(vs.Session)
    s.windows = vs.parse_windows("lowband:hf:42.6:safety,vhfhigh:vhf:155:safety,approach:vhf:135.7,towers:vhf:120.6")
    s.w, s.dwell, s.window_since, s.keyed_since, s.last_keyed = 2, 300, 0.0, {}, {}
    moves = []
    monkeypatch.setattr(s, "set_window", lambda w: (moves.append(s.windows[w].name), setattr(s, "w", w), setattr(s, "window_since", 1e9)))
    s.focus = None
    s.maybe_switch(400.0)
    assert moves == ["towers"]                        # no focus: the normal rotation
    s.focus, s.window_since = "safety", 0.0
    s.maybe_switch(400.0)
    assert moves[-1] == "lowband"                     # held on public safety: skips approach
    s.window_since = 0.0
    s.maybe_switch(400.0)
    assert moves[-1] == "vhfhigh"
    s.window_since = 0.0
    s.maybe_switch(400.0)
    assert moves[-1] == "lowband"                     # ...and back, never leaving public safety
    s.focus, s.window_since = "hf", 0.0
    n = len(moves)
    s.maybe_switch(400.0)
    assert len(moves) == n                            # the only HF window is the current one: stay
    s.focus = "pause"
    assert s.idle


def test_sstv_live_picture_builds_up_then_clears(tmp_path):
    saved = []
    store = SimpleNamespace(add_sstv=lambda **r: saved.append(r), event=lambda *a, **k: None)
    mon = sstvmon.SstvMonitor.__new__(sstvmon.SstvMonitor)
    mon.store, mon.dir = store, tmp_path
    mon.radio = _sstv_radio()
    w = sstvmon.Watch("20m", 14.230e6, "usb")
    audio = _sstv_audio()
    step = int(sstvmon.CHECK_EVERY * sstvmon.RATE)
    rows = []

    async def run():
        t = 1_800_000_000.0
        for i in range(0, len(audio), step):
            w.add(audio[i:i + step].tobytes())
            t += sstvmon.CHECK_EVERY
            await mon.check(w, t)
            if w.live:
                rows.append(w.live["rows"])
                assert (tmp_path / "live_20m.png").exists() and w.live["mode"] == "MARTIN_1"
    asyncio.run(run())
    assert len(rows) >= 5 and rows == sorted(rows) and rows[0] < rows[-1]     # it grows line by line
    assert w.live is None and not (tmp_path / "live_20m.png").exists()          # cleared when the picture is saved
    assert len(saved) == 1


# ---- FT4 ----------------------------------------------------------------------------
GEN = ft8.ROOT / "tools" / "ft8_lib" / "gen_ft8"


@pytest.mark.skipif(not (ft8.DECODER.exists() and GEN.exists()), reason="needs ft8_lib's decode_ft8 and gen_ft8")
def test_ft4_slot_decodes_once_with_on_time_dt(tmp_path):
    import subprocess, wave as _wave
    subprocess.run([str(GEN), "CQ K6ABC CM87", str(tmp_path / "g.wav"), "1500", "-ft4"], capture_output=True, check=True)
    with _wave.open(str(tmp_path / "g.wav")) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
    nz = np.flatnonzero(np.abs(x) > 50)
    sig = x[nz[0]:nz[-1] + 1]
    a = np.zeros(int(7.5 * ft8.RATE))
    a[int(0.5 * ft8.RATE):int(0.5 * ft8.RATE) + len(sig)] = sig * 0.5          # on time: 0.5 s into the slot
    a = np.clip(a + np.random.default_rng(1).normal(0, 300, len(a)), -32767, 32767).astype("<i2")
    saved = []
    mon = ft8.Ft8.__new__(ft8.Ft8)
    mon.proto, mon.tmp, mon._sem = ft8.FT4, tmp_path, asyncio.Semaphore(2)
    mon.store = SimpleNamespace(add_ft8=lambda t, band, dial, rows, table: saved.append((table, rows)))
    feed = ft8.Feed("20m", 14.080e6)
    feed.cut = lambda t0, seconds: a
    asyncio.run(mon._decode(feed, 1_800_000_000.0))
    (table, rows), = saved
    assert table == "ft4" and [r["msg"] for r in rows] == ["CQ K6ABC CM87"]   # both passes found it: kept once
    assert abs(rows[0]["dt"]) < 0.2 and rows[0]["from_call"] == "K6ABC"


# ---- marine: AIS vessels, marine profile, on-demand windows --------------------------------
from rxapp.ais import Vessels, category


def test_ais_vessels_from_aiscatcher_json():
    v = Vessels()
    t0 = 1_800_000_000.0
    # AIS-catcher -o 5 output for real sample messages (position report, then static data)
    v.ingest({"class": "AIS", "type": 1, "mmsi": 366053209, "status_text": "Restricted maneuverability", "speed": 0,
              "lon": -122.341614, "lat": 37.80212, "course": 219.3, "heading": 1, "channel": "B"}, t0)
    v.ingest({"class": "AIS", "type": 5, "mmsi": 351759000, "callsign": "3FOF8", "shipname": "EVER DIADEM ",
              "shiptype": 70, "shiptype_text": "Cargo ships - all ships of this type", "destination": "NEW YORK"}, t0 + 1)
    v.ingest({"class": "AIS", "type": 1, "mmsi": 351759000, "lat": 37.81, "lon": -122.40, "speed": 12.1, "course": 95}, t0 + 2)
    v.ingest({"class": "AIS", "type": 1, "mmsi": 351759000, "lat": 91.0, "lon": 181.0, "speed": 12.1}, t0 + 3)   # "not available"
    snap = {x["mmsi"]: x for x in v.snapshot()["vessels"]}
    ship = snap[351759000]
    assert ship["shipname"] == "EVER DIADEM" and ship["category"] == "cargo" and ship["destination"] == "NEW YORK"
    assert (ship["lat"], ship["lon"]) == (37.81, -122.40) and len(ship["track"]) == 1
    assert snap[366053209]["status_text"] == "Restricted maneuverability"
    assert category(80) == "tanker" and category(52) == "tug" and category(None) == "other"
    v.prune(t0 + 7 * 3600)
    assert not v.v


def test_marine_profile_skips_ais_dsc_and_weather():
    inside = lambda f: any(a <= f <= b for a, b in vs.MARINE.ranges)
    assert inside(156.8e6) and inside(156.7e6) and inside(157.1e6)          # ch16, VTS ch14, USCG 22A
    assert not inside(156.525e6)                                             # ch70: digital DSC
    assert not inside(161.975e6) and not inside(162.025e6)                   # AIS
    assert not inside(162.4e6)                                               # NOAA weather (listen only)


def test_on_demand_window_only_while_held():
    s = vs.Session.__new__(vs.Session)
    s.windows = vs.parse_windows("approach:vhf:135.7,marine:vhf:159.9:marine:hold")
    assert s.windows[1].on_demand and s.windows[1].profile.service == "marine"
    s.focus = None
    assert s.allowed(s.windows[0]) and not s.allowed(s.windows[1])
    s.focus = "marine"
    assert s.allowed(s.windows[1]) and not s.allowed(s.windows[0])


# ---- WSPR ----------------------------------------------------------------------------
from rxapp import wspr

WSPRSIM = wspr.WSPRD.parent / "wsprsim"


def test_wspr_parse_messages_and_distance():
    out = ("1030 -15  0.3  14.097105  0  K6ABC CM87 33\n"
           "1030 -24 -1.1  14.097012  1  PJ4/K1ABC 37\n"
           "1030 -21  0.0  14.096990  0  <PJ4/K1ABC> FK52UD 37\n"
           "<DecodeFinished>\n")
    s = wspr.parse(out, home="FN42")
    assert [(x["call"], x["grid"], x["dbm"]) for x in s] == [("K6ABC", "CM87", 33), ("PJ4/K1ABC", None, 37), ("PJ4/K1ABC", "FK52UD", 37)]
    assert s[0]["snr"] == -15 and s[0]["freq_hz"] == pytest.approx(14.097105e6)
    assert 4000 < s[0]["km"] < 4400                       # CM87 (SF Bay) to FN42 (Boston): ~4,300 km
    lat, lon = wspr.grid_ll("CM87")
    assert (lat, lon) == (37.5, -123.0)


@pytest.mark.skipif(not (wspr.WSPRD.exists() and WSPRSIM.exists()), reason="needs wsprd and wsprsim (tools/wsjtx)")
def test_wspr_slot_decodes_a_beacon_below_the_noise(tmp_path, monkeypatch):
    import re as _re, subprocess
    syms = subprocess.run([str(WSPRSIM), "-c", "K6ABC CM87 33"], capture_output=True, text=True).stdout
    sym = [int(c) for c in _re.findall(r"[0-3]", syms.split("Channel symbols:")[-1])][:162]
    fs, n = wspr.RATE, 8192
    ph, parts = 0.0, []
    for s_ in sym:                                        # 4-FSK, 1.46 Hz spacing, 0.683 s symbols, at 1500 Hz
        f = 1500 - 1.5 * fs / n + s_ * fs / n
        parts.append(np.sin(ph + 2 * np.pi * f * np.arange(n) / fs))
        ph = (ph + 2 * np.pi * f * n / fs) % (2 * np.pi)
    a = np.zeros(fs * 120)
    a[fs:fs + n * len(sym)] = np.concatenate(parts) * 0.05
    a = a + np.random.default_rng(2).normal(0, 0.3, len(a))
    a = (a / np.abs(a).max() * 20000).astype("<i2")
    mon = wspr.WsprMonitor.__new__(wspr.WsprMonitor)
    mon.tmp, mon._sem = tmp_path, asyncio.Semaphore(1)
    rows = []

    class DB:
        def executemany(self, sql, r): rows.extend(r)
        def commit(self): pass
    mon.store = SimpleNamespace(lock=__import__("threading").Lock(), db=DB())
    monkeypatch.setattr(wspr.RUNTIME, "qth", "CM87")
    feed = ft8.Feed("20m", 14.0956e6)
    feed.cut = lambda t0, seconds: a
    asyncio.run(mon._decode(feed, 1_800_000_000.0))
    (row,) = rows
    assert row[1] == "20m" and row[6:9] == ("K6ABC", "CM87", 33) and row[3] < -10   # heard at about -15 dB SNR


# ---- APRS -------------------------------------------------------------------------------
from rxapp.aprs import Stations, kind as aprs_kind


def test_aprs_stations_track_positions_weather_and_objects():
    import aprslib
    st = Stations()
    t0 = 1_800_000_000.0
    for i, tnc2 in enumerate([
            "K6ABC-9>APRS,WIDE1-1,WIDE2-1:!3745.12N/12224.50W>Testing rxapp",
            "K6ABC-9>APRS,WIDE1-1:!3745.30N/12224.10W>Testing rxapp",
            "KJ6WX>APRS:@231530z3746.00N/12223.00W_270/005g010t061r000p000h80b10150",
            "N6EOC>APRS:;EVENT    *231530z3747.00N/12225.00W[Bay to Breakers aid station"]):
        st.ingest(aprslib.parse(tnc2), t0 + i)
    car, wx, obj = st.s["K6ABC-9"], st.s["KJ6WX"], st.s["EVENT"]
    assert car["kind"] == "car" and len(car["track"]) == 2 and car["lat"] == pytest.approx(37.755)
    assert wx["kind"] == "weather" and wx["weather"]["temperature"] == pytest.approx(16.1, abs=0.2)   # 61 F
    assert obj["kind"] == "person" and obj["via"] == "N6EOC"
    assert aprs_kind("\\", "#") == "digipeater" and aprs_kind("/", "-") == "house"
    st.prune(t0 + 13 * 3600)
    assert not st.s


@pytest.mark.skipif(not vs.MULTIMON.exists(), reason="needs tools/multimon-ng")
def test_aprs_audio_decodes_through_multimon():
    import subprocess, aprslib
    sys.path.insert(0, str(Path(__file__).parent))
    from afsk import frame, afsk
    pk = frame("K6ABC-9", "APRS", ["WIDE1-1"], "!3745.12N/12224.50W>rxapp test")
    a = np.concatenate([np.zeros(11025), afsk(pk) * 0.5, np.zeros(11025)])
    a = (a + np.random.default_rng(0).normal(0, 0.05, len(a))) * 20000
    out = subprocess.run([str(vs.MULTIMON), "-t", "raw", "-a", "AFSK1200", "-A", "-"], input=a.astype("<i2").tobytes(),
                         capture_output=True).stdout.decode()
    line = next(l for l in out.splitlines() if l.startswith("APRS: "))
    p = aprslib.parse(line[6:])
    assert p["from"] == "K6ABC-9" and p["latitude"] == pytest.approx(37.752, abs=1e-3)


def test_wspr_feed_keeps_a_whole_two_minute_slot(monkeypatch):
    feed = ft8.Feed("20m", 14.0956e6, buffer_sec=wspr.SLOT_SEC + 30)
    t_start = 1_800_000_000.0                               # an even minute
    _packets(feed, t_start - 2, 125, monkeypatch=monkeypatch)
    assert feed.cut(t_start, wspr.SLOT_SEC) is not None     # all 120 s still there
    short = ft8.Feed("20m", 14.0956e6)                      # the FT8 default keeps 40 s: not enough
    _packets(short, t_start - 2, 125, monkeypatch=monkeypatch)
    assert short.cut(t_start, wspr.SLOT_SEC) is None


# ---- broadcast ----------------------------------------------------------------------------
from rxapp import broadcast


def test_eibi_season_days_and_on_air(tmp_path, monkeypatch):
    import datetime as _dt
    assert broadcast.season(_dt.date(2026, 10, 23)) == "a26" and broadcast.season(_dt.date(2026, 10, 26)) == "b26"
    assert broadcast.season(_dt.date(2027, 1, 5)) == "b26" and broadcast.season(_dt.date(2027, 4, 1)) == "a27"
    assert broadcast._days("Mo-Fr") == {1, 2, 3, 4, 5} and broadcast._days("Sa,Su") == {6, 7} and broadcast._days("") is None
    (tmp_path / "sked-a26.csv").write_text(
        "kHz:75;Time(UTC):93;Days:59;ITU:49;Station:201;Lng:49;Target:62;Remarks:135;P:35;Start:60;Stop:60;\n"
        "9410;0000-0100;;G;BBC;E;NAm;;1;;\n"
        "9455;2300-0100;Sa,Su;USA;WRMI Radio Miami Int.;E;CNA;;1;;\n", encoding="latin-1")
    e = broadcast.Eibi.__new__(broadcast.Eibi)
    e.folder, e.entries, e.file = tmp_path, [], None
    monkeypatch.setattr(broadcast.Eibi, "fetch", lambda self, s: False)   # no network in tests
    e.load()
    sat_0030 = _dt.datetime(2026, 10, 24, 0, 30, tzinfo=_dt.timezone.utc).timestamp()
    mon_0030 = _dt.datetime(2026, 10, 26, 0, 30, tzinfo=_dt.timezone.utc).timestamp()
    assert [x["station"] for x in e.on_air(when=sat_0030)] == ["BBC", "WRMI Radio Miami Int."]   # past midnight
    assert [x["station"] for x in e.on_air(when=mon_0030)] == ["BBC"]                             # weekends only
    assert e.on_air(when=sat_0030)[0]["language"] == "English" and e.on_air(when=sat_0030)[0]["target_name"] == "North America"


def test_broadcast_station_tables():
    s = broadcast.stations()
    assert {x["call"] for x in s["am"]} >= {"KCBS", "KNBR"}
    assert any(x["call"] == "KQED-FM" and x["freq_hz"] == 88_500_000 for x in s["fm"])


# ---- SSB voice ---------------------------------------------------------------------------
from rxapp import ssb


def test_ssb_channels_and_passband():
    d = ssb.channels(32e6)
    assert {c["group"] for c in d["channels"]} == {"ham", "air", "mil", "marine"}
    assert all(c["mode"] in ("usb", "lsb") for c in d["channels"])
    six = next(c for c in d["channels"] if c["freq_hz"] == 50125e3)
    assert six["reachable"] is False                     # 6 m needs the 0-64 MHz coverage
    assert next(c for c in ssb.channels(64.8e6)["channels"] if c["freq_hz"] == 50125e3)["reachable"]
    # a USB voice signal 1 kHz above the dial: seen as USB, not as LSB on the same dial
    f = np.arange(8.95e6, 9.05e6, 250.0)
    db = np.full(f.shape, -110.0)
    db[(f >= 8992.8e3) & (f <= 8994.0e3)] = -90.0
    assert ssb.passband_snr(f, db, 8992e3, "usb") > 15
    assert ssb.passband_snr(f, db, 8992e3, "lsb") < 3


def test_ssb_check_skips_channels_a_band_search_covered():
    from rxapp import server
    from rxapp.jobs import BANDS
    d = ssb.channels(64.8e6)
    for h in d["ham_bands"]:                             # the edges the band search uses
        assert (h["lo_hz"], h["hi_hz"]) == (BANDS[h["band"]].start_hz, BANDS[h["band"]].stop_hz)
    b20 = BANDS["20m"]
    left = {c["freq_hz"] for c in server._ssb_items(server.SsbCheck(group="ham", skip=[[b20.start_hz, b20.stop_hz]]))}
    assert not left & {14285e3, 14300e3, 14325e3}         # inside 20 m: covered by its search
    assert {5357e3, 7285e3} <= left                      # 60 m (never searched) and 40 m (not this time)
    assert len(server._ssb_items(server.SsbCheck(group="ham"))) == sum(c["group"] == "ham" and c["reachable"] for c in d["channels"])


# ---- panels: last pictures survive a restart -------------------------------------------------
def test_panel_history_round_trip(tmp_path, monkeypatch):
    from rxapp import panels
    monkeypatch.setattr(panels, "HISTORY_FILE", tmp_path / "h.npz")
    def hub():
        h = panels.PanelHub.__new__(panels.PanelHub)
        h.hf, h.vhf_history, h._next = {}, {}, 1
        return h
    a = hub()
    a.vhf_history[135656] = (131.4e6, 4101.5, np.arange(2048, dtype=np.float32), 1.0)
    a.feed_for(2e6, 30e6).latest = (2e6, 13672.0, np.ones(2048, np.float32), 2.0)
    a.save()
    b = hub(); b.load()
    assert list(b.vhf_history) == [135656] and b.vhf_history[135656][2][5] == 5
    assert b.hf[(2e6, 30e6)].latest[3] == 2.0


# ---- RTP: channels per packet, learned from the payload type ----------------------------------
def test_rtp_learns_stereo_payload_types():
    from rxapp.rtp import RtpHub
    h = RtpHub("127.0.0.1", 5004)
    ts = 0
    for seq in range(3):                       # mono: 480 frames, 480 samples, PT 120
        h.learn(7, 120, seq, ts, 480); ts += 480
    assert h.pt_channels[120] == 1 and h.channels(7) == 1
    for seq in range(3, 6):                    # the pilot appears: stereo, 480 frames, 960 samples, PT 121
        h.learn(7, 121, seq, ts, 960); ts += 480
    assert h.pt_channels[121] == 2 and h.channels(7) == 2 and h.stereo_packets[7] == 2
    h.learn(7, 120, 6, ts, 480)                # and back to mono, already known
    assert h.channels(7) == 1


# ---- CW skimmer -------------------------------------------------------------------------------------------
def _morse_audio(signals, seconds, rate=48000, noise=0.05, seed=1):
    """Synthetic band: (audio Hz, wpm, amplitude, text, start s) keyed with 5 ms soft edges, in white noise."""
    from rxapp.cw import TO_MORSE
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    x = rng.normal(0, noise, n)
    for hz, wpm, amp, text, start in signals:
        u, runs = 1.2 / wpm, []
        for w, word in enumerate(text.split(" ")):
            if w:
                runs.append((0, 7 * u))
            for c, ch in enumerate(word):
                if c:
                    runs.append((0, 3 * u))
                for e, el in enumerate(TO_MORSE[ch]):
                    if e:
                        runs.append((0, u))
                    runs.append((1, (u if el == "." else 3 * u) * (1 + rng.normal(0, 0.08))))
        env = np.concatenate([np.full(int(d * rate), k, float) for k, d in runs])
        k = np.hanning(int(0.01 * rate)); env = np.convolve(env, k / k.sum(), "same")
        i0 = int(start * rate); env = env[:n - i0]
        x[i0:i0 + len(env)] += amp * env * np.sin(2 * np.pi * hz * t[i0:i0 + len(env)])
    return (x / np.abs(x).max() * 20000).astype(np.int16)


def _skim(x, rate=48000):
    from rxapp.cw import Skimmer
    sk = Skimmer(rate, 200, rate / 2 - 200)
    for i in range(0, len(x), rate):
        sk.feed(x[i:i + rate], i / rate + 1)
    return {round(sk.hz(t.bin + t.frac)): t for t in list(sk.tones.values()) + sk.ended}


def test_cw_skimmer_decodes_several_signals_at_once():
    x = _morse_audio([(3000, 22, 0.2, "CQ CQ DE K6ABC K6ABC K", 1.0),
                      (3150, 15, 0.05, "CQ TEST W1AW W1AW TEST", 0.5),       # 150 Hz away, weaker, slower
                      (9000, 32, 0.02, "VVV DE DL1XYZ TNX FER QSO 73 TU", 2.0)], 26)
    tones = _skim(x)
    texts = {hz: t.text for hz, t in tones.items()}
    near = lambda f: next(t for hz, t in tones.items() if abs(hz - f) < 25)
    assert "CQ CQ DE K6ABC K6ABC K" in near(3000).text, texts
    assert "W1AW W1AW TEST" in near(3150).text, texts
    assert "DL1XYZ TNX FER QSO 73 TU" in near(9000).text, texts
    assert abs(near(3000).wpm - 22) < 3 and abs(near(3150).wpm - 15) < 2 and abs(near(9000).wpm - 32) < 4


def test_cw_skimmer_ignores_noise_and_carriers():
    x = _morse_audio([], 40, seed=3).astype(float)
    x += 3000 * np.sin(2 * np.pi * 5000 * np.arange(len(x)) / 48000)
    tones = _skim(x.astype(np.int16))
    assert not [t for t in tones.values() if t.chars >= 3], {hz: t.text for hz, t in tones.items()}


def test_cw_unit_estimate_and_spots():
    from rxapp.cw import spots_in, unit_estimate
    # dots, inner gaps (1), dashes and letter gaps (3), a word gap (7): the unit is 10
    assert abs(unit_estimate([10, 11, 9, 30, 31, 29, 10, 70, 10, 30]) - 10) < 1.5
    assert unit_estimate([10, 10]) is None
    assert spots_in("CQ CQ DE K6ABC K6ABC K") == ["K6ABC"]
    assert spots_in("TEST W1AW W1AW") == ["W1AW"]
    assert spots_in("CQ POTA DE VE3/W1XYZ VE3/W1XYZ") == ["VE3/W1XYZ"]
    assert spots_in("CQ SS N9RV CQ SS N9RV") == ["N9RV"]
    assert spots_in("CQ AL4A CQ AL4E CQ AL4A") == ["AL4A"]          # AL4E: decoded once, a bust
    assert spots_in("5NN TU DE 73 EE") == []
    assert spots_in("GE OM DE DL1XYZ/P DL1XYZ/P") == ["DL1XYZ/P"]


def test_ndjson_streams_partials_then_the_result():
    """Streaming endpoints (scans): one JSON line per partial result, then {"done": ...} or {"error": ...}."""
    import asyncio
    import json as _json
    from rxapp.server import _ndjson

    async def good(emit):
        for i in range(3):
            emit([{"freq_hz": 1e6 + i}])
            await asyncio.sleep(0)
        return {"hits": 3}

    async def bad(emit):
        emit([1])
        raise ValueError("switch the input to HF first")

    async def lines(run):
        out = []
        async for chunk in _ndjson(run).body_iterator:
            out += [_json.loads(x) for x in (chunk if isinstance(chunk, str) else chunk.decode()).splitlines()]
        return out

    assert asyncio.run(lines(good)) == [{"partial": [{"freq_hz": 1e6}]}, {"partial": [{"freq_hz": 1e6 + 1}]},
                                        {"partial": [{"freq_hz": 1e6 + 2}]}, {"done": {"hits": 3}}]
    assert asyncio.run(lines(bad)) == [{"partial": [1]}, {"error": "switch the input to HF first"}]


# ------------------------------------------------------------------ public-safety incidents

def _jx(tmp_path):
    from rxapp.junctions import Junctions
    import json
    p = tmp_path / "j.json"
    p.write_text(json.dumps({"highways": {
        "880": [{"name": "Brokaw Road", "pts": [[37.375, -121.905]]},
                {"name": "Auto Mall Parkway", "pts": [[37.505, -121.965]]},
                {"name": "Stevenson Boulevard", "pts": [[37.535, -121.985]]}],
        "580": [{"name": "Dougherty Road", "pts": [[37.705, -121.925]]},
                {"name": "1st Street", "pts": [[37.700, -121.760]]}],
        "101": [{"name": "Oakland Road", "pts": [[37.370, -121.895]]},
                {"name": "Poplar Avenue", "pts": [[37.575, -122.320]]}]}}))
    return Junctions(p)


def test_incident_kinds_from_codes_and_words():
    from rxapp.incidents import kind
    k = lambda t: [x["label"] for x in kind(t)]
    assert k("all units, a 23152 heading south on 101 near Poplar") == ["DUI"]
    assert k("new call, an 11-83 on southbound 880") == ["Collision"]
    assert k("1180 northbound 680")[0] == "Collision, major injuries"
    assert k("11-25 for debris on westbound 92") == ["Road hazard"]
    assert k("12-345, that is for San Jose fire.") == []                  # the fire department, not a fire
    assert k("a pickup with all-around fire damage")[0] == "Vehicle fire"
    assert k("attention units, a 23-103 on northbound 880") == ["Reckless driver"]


def test_incident_status_and_units():
    from rxapp.incidents import status, units
    assert status("12-345, I'm 97.") == ["onscene"]
    assert "enroute" in status("1234, copy, show you en route")
    assert "clear" in status("1234, I'll be 98.")
    assert "tow" in status("requesting 1185 for the sedan")
    assert units("12-3-45, I copy.") == ["12345"]
    assert units("1234 27 booked") == ["1234"]
    assert units("25 units broadcast for") == []


def test_incident_locations_at_highway_junctions(tmp_path):
    from rxapp.incidents import locate
    jx = _jx(tmp_path)
    p = locate("11-25, southbound 80 just north of Brokaw, a ladder", jx, 42500000)
    assert p[0]["hwy"] == "880" and p[0]["street"] == "Brokaw Road" and p[0]["corrected"]
    p = locate("a 23-103 westbound 580 east of Doherty", jx, 44942000)
    assert p[0]["street"] == "Dougherty Road"
    p = locate("a pedestrian near the Auto Mall Parkway on ramp to northbound 880", jx, 44942000)
    assert p[0]["street"] == "Auto Mall Parkway" and p[0]["radius_km"] < 1
    p = locate("11-25 North 911 south of Oakland Road", jx, 42502000)
    assert p[0]["hwy"] == "101"
    p = locate("weaving, Northbound Navy just off the Stevenson", jx, 44942000)
    assert p[0]["street"] == "Stevenson Boulevard"
    p = locate("on a stop, First Street just south of 580", jx, 44942000)
    assert p[0]["street"] == "1st Street"
    assert locate("1234 Example Court Hayward", jx, 44942000)[0]["kind"] == "city"   # never the house number
    assert locate("southbound 880 north of Nowhere Lane", jx, 42500000) == []


def test_incidents_group_by_place_and_unit_and_close(tmp_path):
    from rxapp.incidents import build
    jx = _jx(tmp_path)
    T = 1_800_000_000
    recs = [
        {"i": 0, "t0": T, "t1": T + 5, "freq_hz": 42500000, "text": "1234, 11-25, southbound 880 just north of Brokaw, a ladder in lane 2."},
        {"i": 1, "t0": T + 60, "t1": T + 63, "freq_hz": 42500000, "text": "1234, I copy, en route."},
        {"i": 2, "t0": T + 90, "t1": T + 95, "freq_hz": 42502000, "text": "now an 1183 southbound 880 north of Brokaw, two cars"},
        {"i": 3, "t0": T + 300, "t1": T + 302, "freq_hz": 42500000, "text": "1234, I'm 97."},
        {"i": 4, "t0": T + 400, "t1": T + 404, "freq_hz": 42500000, "text": "All units, a 23-103 westbound 580 east of Doherty, plate 1 Adam Boy Charles 234."},
        {"i": 5, "t0": T + 900, "t1": T + 903, "freq_hz": 42500000, "text": "1234, I'll be 98."},
        {"i": 6, "t0": T + 950, "t1": T + 953, "freq_hz": 42500000, "text": "Okay."},
    ]
    inc = build(recs, jx, now=T + 1000)
    assert len(inc) == 2
    a, b = inc
    assert a["cat"] == "collision" and a["place"]["label"].endswith("Brokaw Road")   # the most serious type seen
    assert [e["i"] for e in a["entries"]] == [0, 1, 2, 3, 5] and a["units"] == ["1234"]
    assert a["state"] == "closed" and a["closed_by"] == "cleared" and "onscene" in a["status"]
    assert b["label"] == "Reckless driver" and b["state"] == "open"
    assert "Adam" not in b["entries"][0]["text"] and "[plate]" in b["entries"][0]["text"]
    assert build(recs, jx, now=T + 900 + 2 * 3600)[1]["closed_by"] == "quiet"


def test_incident_text_redaction():
    from rxapp.incidents import redact
    assert redact("1234, 27 returns clear to a 2015 sedan, last of Example") == "(records check: not shown)"
    assert redact("call back on 555-0100.").endswith("[number].")
    assert "[address]" in redact("damage at 1234 Example Court Hayward")
    assert redact("show Officer Example at 1015") == "show Officer [name] at 1015"
    assert "999" not in redact("sedan, plate ending with 999, weaving")
    assert redact("11-25 northbound 101 south of Holly") == "11-25 northbound 101 south of Holly"


# -- spectrum channel lifetimes -------------------------------------------------------------------
from rxapp import reap
from rxapp.spectrum import END_FRAMES, LeasedSpectrumStream


class _Ctl:
    def __init__(self):
        self.calls = []

    def set_spectrum(self, ssrc, **kw):
        self.calls.append(("spectrum", ssrc))

    def set_channel_lifetime(self, ssrc, lifetime, encoding=None):
        self.calls.append(("lifetime", ssrc, lifetime))

    def remove_channel(self, ssrc):
        self.calls.append(("remove", ssrc))


def test_spectrum_stream_is_leased_renewed_and_ended(monkeypatch):
    """Without a lifetime radiod keeps a stopped spectrum channel forever (remove_channel only zeroes it)."""
    ctl, clock = _Ctl(), [100.0]
    monkeypatch.setattr("rxapp.spectrum.time.monotonic", lambda: clock[0])
    s = LeasedSpectrumStream(control=ctl, frequency_hz=1e6, lifetime=1500, renew_sec=10)
    s._ssrc, s._running = 42, True
    s._send_spectrum_command()                    # creation: leased at once
    clock[0] += 5
    s._send_spectrum_command()                    # a poll: no renewal yet
    clock[0] += 6
    s._send_spectrum_command()                    # renewal due
    assert [c for c in ctl.calls if c[0] == "lifetime"] == [("lifetime", 42, 1500)] * 2
    s.stop()
    assert ctl.calls[-2:] == [("remove", 42), ("lifetime", 42, END_FRAMES)]
    n = len(ctl.calls)
    s.stop()                                      # second stop: nothing more sent
    assert len(ctl.calls) == n


def test_reap_only_takes_unowned_spectrum_channels_at_zero():
    C = SimpleNamespace
    chans = [C(ssrc=1, demod_type=4, frequency=0.0),       # orphan
             C(ssrc=2, demod_type=4, frequency=4.57e6),    # live spectrum stream
             C(ssrc=3, demod_type=0, frequency=0.0),       # parked audio channel
             C(ssrc=4, demod_type=4, frequency=0.0),       # owned
             C(ssrc=5, demod_type=3, frequency=0.0)]       # old-style spectrum orphan
    assert reap.orphans(chans, owned={4}) == {1, 5}


def test_agc_seed_starts_a_new_channel_near_its_neighbours_gain():
    """A fresh channel's AGC climbs from low gain for ~4 s of silence unless it is given a starting gain."""
    ft8 = Channel(1, 14.074e6, "usb", kind="ft8", gain_db=81.0)
    cw40 = Channel(2, 7.010e6, "usb", kind="cw", gain_db=78.0)
    fm = Channel(3, 39.9e6, "nfm", gain_db=20.0)
    vhf = Channel(4, 120.5e6, "am", band="vhf", gain_db=60.0)
    new = Channel(5, 14.030e6, "cwu")
    assert agc_seed(new, [ft8, cw40, fm, vhf, new]) == 91.0            # nearest HF AGC channel + margin
    assert agc_seed(Channel(6, 39.9e6, "nfm"), [ft8]) is None           # FM has no AGC
    assert agc_seed(Channel(7, 7.0e6, "lsb", gain_db=70.0), [ft8]) == 70.0   # re-created: its own gain
    assert agc_seed(new, [Channel(8, 50.3e6, "usb", gain_db=97.0)]) == 100.0  # radiod's limit
    assert agc_seed(new, [new]) == 90.0                                  # nothing known yet: HF default
    assert agc_seed(Channel(9, 120.5e6, "am", band="vhf"), []) is None  # VHF: no guess


# ---- ADS-B from this receiver (voice_session.AdsbHopper) merged into /api/adsb -------------------------
def test_adsb_hopper_keeps_identity_position_and_trail(tmp_path):
    h = vs.AdsbHopper(tmp_path)
    h._ingest({"icao": "A12345", "df": 17, "crc_valid": True, "callsign": "TEST123_"}, 1000.0)
    h._ingest({"icao": "A12345", "df": 17, "altitude": 4500, "latitude": 37.61, "longitude": -122.38}, 1001.0)
    h._ingest({"icao": "A12345", "df": 17, "groundspeed": 180.4, "track": 284.6, "vertical_rate": -640}, 1001.5)
    h._ingest({"icao": "A12345", "df": 17, "altitude": 4300, "latitude": 37.62, "longitude": -122.40}, 1031.0)
    # a position decoded against a reference too far away lands in the wrong zone: not kept
    h._ingest({"icao": "A12345", "df": 17, "latitude": 43.0, "longitude": -122.4}, 1032.0)
    h.write(1040.0)
    snap = _json.loads((tmp_path / "adsb.json").read_text())
    (a,) = snap["aircraft"]
    assert a["hex"] == "a12345" and a["flight"] == "TEST123" and a["messages"] == 5
    assert (a["lat"], a["lon"], a["alt"], a["gs"], a["track"], a["rate"]) == (37.62, -122.4, 4300, 180, 285, -640)
    assert [p[0] for p in a["trail"]] == [1001.0, 1031.0] and a["heard"] == 1032.0
    h.write(1032.0 + h.KEEP_SEC + 1)                   # not heard for 15 min: forgotten
    assert _json.loads((tmp_path / "adsb.json").read_text())["aircraft"] == []


def test_adsb_merge_marks_heard_here_and_adds_unlisted_aircraft():
    from rxapp.server import merge_adsb
    feed = {"now": 2000.0, "source": "adsb.fi", "aircraft": [
        {"hex": "a12345", "flight": "TEST123", "lat": 37.6, "lon": -122.4, "trail": []},
        {"hex": "b00001", "flight": "OTHER1", "lat": 37.7, "lon": -122.2, "trail": []}]}
    local = {"t": 1990.0, "hops": 4, "every": 30, "secs": 2, "messages": 9, "aircraft": [
        {"hex": "a12345", "flight": "TEST123", "lat": 37.61, "lon": -122.41, "heard": 1985.0, "messages": 6, "t": 1985.0, "trail": []},
        {"hex": "c00002", "flight": "", "lat": 37.5, "lon": -122.1, "alt": 3000, "heard": 1980.0, "messages": 3, "t": 1980.0,
         "trail": [[1900.0, 37.4, -122.0, 2500], [1980.0, 37.5, -122.1, 3000]]},
        {"hex": "d00003", "flight": "NOPOS1", "lat": None, "lon": None, "heard": 1988.0, "messages": 1, "t": 0.0, "trail": []}]}
    out = merge_adsb(feed, local, since=1950.0)
    by = {a["hex"]: a for a in out["aircraft"]}
    assert by["a12345"]["heard_here"] == 1985.0 and by["a12345"]["lat"] == 37.6       # the feed's position, marked as heard
    assert "heard_here" not in by["b00001"]
    assert by["c00002"]["only_here"] and by["c00002"]["trail"] == [[1980.0, 37.5, -122.1, 3000]]   # trail since `since`
    assert "d00003" not in by                                                           # no position: not on the map
    assert out["local"]["hops"] == 4 and merge_adsb(feed, {})["local"] is None


def test_adsb_hops_grow_when_voice_is_quiet(tmp_path):
    h = vs.AdsbHopper(tmp_path)                        # default plan
    assert h.plan(0.0) == (5.0, 10)                    # silent window: 5 s every 10 s
    assert h.plan(0.075) == (4.0, 15)                  # in between: interpolated
    assert h.plan(0.15) == (3.0, 20)
    assert h.plan(0.225) == (2.5, 25)
    assert h.plan(0.3) == h.plan(0.9) == (2.0, 30)     # busy: 2 s every 30 s
    assert vs.plan_points("0:5:10,15:3:20,30:2:30") == vs.AdsbHopper.DEFAULT_PLAN and vs.plan_points("off") is None
    with pytest.raises(ValueError):
        vs.plan_points("0:12:10")                      # a hop longer than its interval


def test_adsb_waits_for_a_gap_in_voice(tmp_path, monkeypatch):
    s = vs.Session.__new__(vs.Session)
    s.adsb = vs.AdsbHopper(tmp_path)
    s.activity, s.keyed_since, s.last_keyed, s.focus = vs.deque(), {}, {}, "air"
    s.windows, s.w, s.ctl, s.tuner_hz = [vs.Window("approach", "vhf", 135.7e6, vs.AIR)], 0, None, 135.7e6
    assert s.busy(1000.0) == 1.0                       # no history yet (a new window): counts as busy
    hops = []
    monkeypatch.setattr(s.adsb, "hop", lambda ctl, home: hops.append(home) or setattr(s.adsb, "last", 1e12))
    now = 1000.0
    s.adsb.last = now - 30
    for k in range(1200):                              # the last 2 minutes: on the air a quarter of the time
        s.activity.append((now - 120 + k * 0.1, k % 4 == 0))
    s.last_keyed = {135.65e6: now - 1.0}               # a transmission ended a second ago: a reply may follow
    s.maybe_adsb(now)
    assert not hops and s.adsb.every == 27            # busy 25%: every 27 s; 30 s since the last hop: due
    s.adsb.last = now - 30
    assert not hops                                    # due, but within the gap after the last transmission
    s.last_keyed = {135.65e6: now - 4.0}
    s.maybe_adsb(now)
    assert hops == [135.7e6]


def test_adsb_feed_switch_clears_and_stops_the_feed():
    feed = AdsbFeed(enabled=True)
    feed.ingest({"ac": [{"hex": "a12345", "lat": 37.6, "lon": -122.4, "alt_baro": 3000}]}, now=1000.0)
    assert feed.snapshot()["aircraft"] and feed.snapshot()["feed"] is True
    feed.set_enabled(False)
    snap = feed.snapshot()
    assert snap["aircraft"] == [] and snap["feed"] is False and snap["source"] is None


# ---- P25 (dsd-fme console parsing) ------------------------------------------------------------
P25_CONSOLE = """\x1b[0m20:12:16 Sync: -P25p1  \x1b[36mWACN: BEE00; SYS: 5E4; NAC/CC: 5E1; RFSS: 001; Site: 001; \x1b[0m TSBK
 Identifier Update for TDMA - Abbreviated
  Channel Identifier [3] Channel Type [3] Transmit Offset [2960]
  Channel Spacing [064] Base Frequency [09157562] [762006250]
 Identifier Update (8.3.1.23)
  Channel Identifier [1] BW [68] Transmit Offset [0978]
  Channel Spacing [032] Base Frequency [09157562] [762006250]
 Group Voice Channel Grant
  SVC [04] CHAN [36D0] Group [2351] Source [13180001]
 Encrypted Group Voice Channel Grant
  SVC [44] CHAN [37C1] Group [1801] Source [13330451]
 Group Voice Channel Grant Update - Implicit
  Channel 1 [37C0] Group 1 [2701][0A8D]
 Group Voice Channel Grant Update - Implicit
  Channel 1 [37C0] Group 1 [2701][0A8D]
"""


def test_p25_trunk_parses_system_channel_plan_and_grants():
    from rxapp.p25 import Trunk
    t = Trunk()
    for k, line in enumerate(P25_CONSOLE.splitlines()):
        t.feed(line, 100.0 + k * 0.1)
    snap = t.snapshot(101.5)
    assert snap["system"] == {"wacn": "BEE00", "sysid": "5E4", "nac": "5E1", "rfss": "001", "site": "001"}
    assert snap["locked"] and snap["channel_plan"] == 2
    assert t.iden[3] == {"base": 762006250.0, "spacing": 12500.0, "tdma": True}
    tg = {g["tg"]: g for g in snap["talkgroups"]}
    assert tg[2351]["hz"] == 772906250.0 and tg[2351]["src"] == 13180001 and not tg[2351]["enc"]
    assert tg[1801]["enc"] and tg[1801]["hz"] == 774406250.0           # TDMA: two slots per carrier
    assert tg[2701]["grants"] == 1 and tg[2701]["hz"] == 774406250.0   # a repeated update is the same call
    assert t.freq(0x1000 | 10) == 762006250.0 + 10 * 6250.0           # FDMA identifier: one channel per step


def test_p25_lists_finished_calls_only(tmp_path):
    import os
    from rxapp.p25 import list_calls
    done = tmp_path / "20261108_201709_00651_P25_BEE005E45E1_1_1_GROUP_TGT_1001_SRC_13100321.wav"
    done.write_bytes(b"\0" * (44 + 16000 * 3))
    os.utime(done, (1000, 1000))
    (tmp_path / "20261108_201800_00700_P25_BEE005E45E1_1_1_GROUP_TGT_4798_SRC_1.wav").write_bytes(b"\0" * 100)  # being written
    (tmp_path / "19691231_160000_47090__GROUP_TGT_0_SRC_0.wav").write_bytes(b"\0" * 100)
    calls = list_calls(tmp_path, now=2000)
    assert [c["tg"] for c in calls] == [1001]
    assert calls[0]["src"] == 13100321 and calls[0]["secs"] == 3.0


def test_p25_api_reads_the_latest_session(tmp_path, monkeypatch):
    import json, os
    from fastapi.testclient import TestClient
    from rxapp import server
    d = tmp_path / "p25" / "20261108-2000"
    (d / "calls").mkdir(parents=True)
    (d / "p25.json").write_text(json.dumps({"t": 1, "running": True, "system": {"wacn": "BEE00"}, "talkgroups": [], "grants": []}))
    wav = d / "calls" / "20261108_201709_00651_P25_BEE005E45E1_1_1_GROUP_TGT_1001_SRC_13100321.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 100)
    os.utime(wav, (1000, 1000))
    monkeypatch.setattr(server, "P25_DIR", tmp_path / "p25")
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    assert c.get("/api/p25/trunk").json()["system"]["wacn"] == "BEE00"
    calls = c.get("/api/p25/calls").json()
    assert calls[0]["tg"] == 1001
    assert c.get(f"/api/p25/clip/20261108-2000/{calls[0]['clip']}").status_code == 200
    assert c.get("/api/p25/clip/20261108-2000/..%2Fp25.json").status_code == 404


def test_p25_watchdog_takes_a_stranded_decoder_back_to_the_control_channel(tmp_path):
    from rxapp.p25 import CC_HZ, P25Receiver
    calls = []
    rx = P25Receiver(tmp_path)
    rx.ctl = SimpleNamespace(set_frequency=lambda ssrc, f: calls.append((ssrc, f)))
    rx.running, rx.ssrc, rx.tuner_hz, rx.if_hz, rx.started = True, 7, 773.45e6, 4.57e6, 100.0
    rx.tuned_hz = 774.68125e6                       # left on a voice channel
    rx.trunk.last_sync = 100.0
    rx.watchdog(103.0)                              # 3 s: still fine
    assert not calls
    rx.watchdog(106.0)                              # 6 s without sync: back to the control channel
    assert rx.tuned_hz == CC_HZ and calls == [(7, 4.57e6 - (CC_HZ - 773.45e6))]


def test_uhf_profile_leaves_out_frs_gmrs_and_parses_as_a_window():
    import voice_session as vs
    u = vs.PROFILES["uhf"]
    inside = lambda f: any(a <= f <= b for a, b in u.ranges)
    assert inside(451.39375e6) and inside(462.04375e6) and inside(464.69375e6)
    assert not inside(462.5625e6) and not inside(462.7125e6) and not inside(467.6375e6)   # FRS/GMRS
    (w,) = vs.parse_windows("uhf454:vhf:454.2:uhf:hold")
    assert w.profile is u and w.on_demand and w.band == "vhf"


def test_uhf_api_serves_transmissions(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from rxapp import server
    d = tmp_path / "uhf" / "20261108-2100"
    d.mkdir(parents=True)
    (d / "transmissions.jsonl").write_text(json.dumps({"t0": 1.0, "t1": 3.0, "freq_hz": 451393750.0, "kind": "voice",
                                                       "text": "copy", "wav": str(d / "a.wav")}) + "\n")
    (d / "a.wav").write_bytes(b"RIFF" + b"\0" * 40)
    monkeypatch.setattr(server, "UHF_DIR", tmp_path / "uhf")
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    tx = c.get("/api/uhf/transmissions").json()
    assert tx[0]["text"] == "copy" and tx[0]["clip"] == "a.wav"
    assert c.get("/api/uhf/clip/20261108-2100/a.wav").status_code == 200


def test_p25_marks_calls_encrypted_from_the_voice_channel(tmp_path):
    import os
    from rxapp.p25 import Trunk, list_calls
    t = Trunk()
    for line in [" VCH 0 - TG 1801 SRC 13330003 ", "         ALG ID: 0x84 KEY ID: 0xA100 MI: 0xF53F9EDE46EA779E MPTT",
                 " VCH 1 - TG 2801 SRC 13330010 ", "         ALG ID: 0x80 KEY ID: 0x0000 MI: 0x0 MPTT"]:
        t.feed(line, 1794202000.0)
    assert t.enc_calls[-1][1] == 1801 and len(t.enc_calls) == 1
    import time as _t
    stamp = _t.strftime("%Y%m%d_%H%M%S", _t.localtime(1794201998))
    for tg in (1801, 2801):
        p = tmp_path / f"{stamp}_00001_P25_BEE005E45E1_1_1_GROUP_TGT_{tg}_SRC_1.wav"
        p.write_bytes(b"\0" * (44 + 16000 * 4))
        os.utime(p, (1000, 1000))
    calls = {c["tg"]: c for c in list_calls(tmp_path, now=1794203000, enc_calls=[[1794202000.0, 1801]])}
    assert calls[1801]["enc"] and not calls[2801]["enc"]


def test_voiced_share_tells_speech_from_fm_noise():
    import voice_session as vs
    rate = 12000
    t = np.arange(int(3 * rate)) / rate
    rng = np.random.default_rng(0)
    pitch = 120 * (1 + 0.1 * np.sin(2 * np.pi * 0.7 * t))           # a voice: harmonics of a gliding pitch,
    phase = 2 * np.pi * np.cumsum(pitch) / rate                       # in syllables 4 times a second
    vowel = sum(np.sin(k * phase) / k for k in range(1, 20)) * (np.sin(2 * np.pi * 4 * t) > -0.2)
    speech = (vowel * 6000 + rng.normal(0, 600, len(t))).astype(np.int16)
    noise = (rng.normal(0, 6000, len(t)) * (1 + 0.8 * np.sin(2 * np.pi * 3 * t))).astype(np.int16)   # hiss, even if it pulses
    assert vs.voiced_share(speech, rate) > 0.3
    assert vs.voiced_share(noise, rate) < vs.VOICED_MIN


def test_clean_noise_clears_invented_text(tmp_path):
    import json
    import wave as wv
    import voice_session as vs
    rate = 12000
    rng = np.random.default_rng(1)
    w = tmp_path / "a.wav"
    with wv.open(str(w), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(rate)
        f.writeframes(rng.normal(0, 5000, 3 * rate).astype("<i2").tobytes())
    rec = {"t0": 1.0, "t1": 4.0, "freq_hz": 41102500.0, "kind": "voice", "text": "Okay.", "text_atc": "Okay.", "wav": str(w)}
    (tmp_path / "transmissions.jsonl").write_text(json.dumps(rec) + "\n")
    assert vs.clean_noise(tmp_path) == 1
    last = json.loads((tmp_path / "transcripts.jsonl").read_text().splitlines()[-1])
    assert last["text"] == "" and last["cleaned"] == "no speech"
    assert vs.clean_noise(tmp_path) == 0                             # already clean


# ---- SSB transcription (rxapp/ssbvoice.py) -------------------------------------------------------
def _ssb_voice(sec, f0=120, rate=12000, shift_hz=0.0):
    from scipy.signal import hilbert
    t = np.arange(int(sec * rate)) / rate
    pitch = f0 * (1 + 0.1 * np.sin(2 * np.pi * 0.7 * t))
    ph = 2 * np.pi * np.cumsum(pitch) / rate
    v = sum(np.sin(k * ph) / k for k in range(1, 25))
    v = v / np.sqrt((v ** 2).mean()) * (np.sin(2 * np.pi * 4 * t) > -0.3)        # syllables
    if shift_hz:                                                                   # a mistuned SSB receiver
        v = np.real(hilbert(v) * np.exp(2j * np.pi * shift_hz * t))
    return v


def _ssb_hiss(sec, rng, rate=12000):
    from scipy.signal import butter, sosfiltfilt
    x = sosfiltfilt(butter(4, [300, 2700], "band", fs=rate, output="sos"), rng.standard_normal(int(sec * rate)))
    return x / np.sqrt((x ** 2).mean())


def _ssb_run(sig, rate=12000):
    from rxapp.ssbvoice import Segmenter
    a = (sig / np.abs(sig).max() * 20000).astype(np.int16)
    seg, out = Segmenter(rate), []
    for i in range(0, len(a), 240):                                # 20 ms packets, as radiod sends them
        out += seg.feed(a[i:i + 240], 1000 + (i + 240) / rate)
    return out + seg.flush()


def test_ssb_segmenter_finds_speech_in_hiss_even_mistuned():
    rng = np.random.default_rng(0)
    g = 10 ** (8 / 20)                                             # 8 dB SNR in the voice band
    sig = np.concatenate([_ssb_hiss(5, rng), _ssb_voice(3) * g + _ssb_hiss(3, rng), _ssb_hiss(4, rng),
                          _ssb_voice(2, 220, shift_hz=60) * g + _ssb_hiss(2, rng), _ssb_hiss(3, rng)])
    out = _ssb_run(sig)
    assert len(out) == 2
    (a, b) = [(u["t0"] - 1000, u["t1"] - 1000) for u in out]
    assert 4.0 <= a[0] <= 5.2 and 7.8 <= a[1] <= 8.8                # the 5-8 s voice, with its lead-in
    assert 11.0 <= b[0] <= 12.2 and 13.8 <= b[1] <= 14.8           # the mistuned 12-14 s voice


def test_ssb_segmenter_ignores_hiss():
    assert _ssb_run(_ssb_hiss(120, np.random.default_rng(5))) == []


def test_ssb_transcripts_api(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from rxapp import server
    from rxapp.ssbvoice import SsbTranscriber
    tr = SsbTranscriber(SimpleNamespace(channels={}), SimpleNamespace(subscribe=lambda *a: None, unsubscribe=lambda *a: None),
                        tmp_path)
    day = tmp_path / "20261114"
    day.mkdir()
    rows = [{"t0": 10.0, "t1": 12.0, "freq_hz": 7185000, "mode": "lsb", "text": "", "day": day.name, "clip": "a.wav"},
            {"t0": 20.0, "t1": 23.0, "freq_hz": 7185000, "mode": "lsb", "text": "CQ CQ this is W6ABC", "day": day.name, "clip": "b.wav"}]
    (day / "transcripts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (day / "b.wav").write_bytes(b"RIFF" + b"\0" * 40)
    monkeypatch.setattr(server.S, "ssbv", tr, raising=False)
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    assert [r["text"] for r in c.get("/api/ssb/transcripts").json()] == ["CQ CQ this is W6ABC"]   # no-words hidden
    assert len(c.get("/api/ssb/transcripts?empty=true").json()) == 2
    assert c.get("/api/ssb/transcripts?after=15&empty=true").json()[0]["t0"] == 20.0


# ---- SSB auto-tune (rxapp/ssbtune.py) ------------------------------------------------------------
@pytest.mark.parametrize("f0,shift", [(110, -170), (200, 85), (130, 0), (180, 230)])
def test_ssb_tuning_error_from_the_voice(f0, shift):
    from rxapp.ssbtune import estimate
    rng = np.random.default_rng(1)
    sig = _ssb_voice(4, f0, shift_hz=shift) * 10 ** (12 / 20) + _ssb_hiss(4, rng)
    d, n = estimate(sig, 12000)
    assert n >= 40 and abs(d - shift) <= 6


def test_ssb_hiss_gives_no_tuning_estimate():
    from rxapp.ssbtune import estimate
    assert estimate(_ssb_hiss(5, np.random.default_rng(2)), 12000)[1] < 10


def test_ssb_corrected_dial_sign_and_snap():
    from rxapp.ssbtune import corrected_dial
    assert corrected_dial(7240100, "lsb", 108) == (7240000, True)      # voice too high on LSB: dial too high
    assert corrected_dial(14250000 - 110, "usb", 108) == (14250000, True)
    assert corrected_dial(7183300, "lsb", -64) == (7183360, False)     # off the grid: 10 Hz steps


def test_ssb_autotune_pools_then_moves_then_wants_agreement():
    from rxapp.ssbtune import D, AutoTune
    def sc(d):
        return np.exp(-((D - d) / 6.0) ** 2)
    a = AutoTune()
    a.new_station(7240100)
    assert a.add(sc(108), 20, 7240100, "lsb", 1) is None                # 20 frames: not enough yet
    assert a.add(sc(108), 30, 7240100, "lsb", 2) == 7240000             # pooled: snap to the net
    assert a.add(sc(2), 50, 7240000, "lsb", 3) is None and not a.last["moved"]     # in tune
    assert a.add(sc(-90), 50, 7240000, "lsb", 4) is None and a.last["waiting"]     # second station: wait...
    assert a.add(sc(-92), 50, 7240000, "lsb", 5) == 7240090                         # ...it agrees: move
    a.add(sc(40), 30, 7241000, "lsb", 6)                               # tuned by hand: starts over there
    assert a.frames == 30 and a.dial == 7241000


def test_ssb_transcriber_autotunes_the_channel(tmp_path):
    import asyncio
    from rxapp.ssbvoice import SsbTranscriber
    ch = SimpleNamespace(freq_hz=7240100.0, mode="lsb", sample_rate=12000)
    tuned = []

    async def tune(ssrc, f):
        tuned.append(f)
        ch.freq_hz = f
    radio = SimpleNamespace(channels={5: ch}, tune=tune)
    tr = SsbTranscriber(radio, SimpleNamespace(subscribe=lambda *a: None, unsubscribe=lambda *a: None), tmp_path)

    async def go():
        tr.beat(5, transcribe=False, autotune=True)
        voice = _ssb_voice(3, 140, shift_hz=105) * 10 ** (12 / 20) + _ssb_hiss(3, np.random.default_rng(3))
        tr._queue({"audio": (voice / np.abs(voice).max() * 20000).astype(np.int16), "t0": 0, "t1": 3, "voiced": .5})
        for _ in range(100):
            await asyncio.sleep(0.05)
            if tuned:
                break
    asyncio.run(go())
    assert tuned == [7240000.0] and tr.busy == 0                      # retuned, nothing sent to Whisper
    assert tr.status()["autotune"]["last"]["snapped"]


# ---- Reset stats (web/statsreset.js) -------------------------------------------------------------
def test_stats_reset_api_sets_and_clears_a_page(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from rxapp import server
    from rxapp.config import Runtime
    rt = Runtime(tmp_path / "settings.json")
    monkeypatch.setattr(server, "RUNTIME", rt)
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    t = c.post("/api/stats/reset", json={"page": "ft8"}).json()["ft8"]
    assert abs(t - time.time()) < 5 and Runtime(tmp_path / "settings.json").stats_reset["ft8"] == t   # kept
    assert c.get("/api/stats/reset").json() == {"ft8": t}
    assert c.post("/api/stats/reset", json={"page": "ft8", "on": False}).json() == {}
    assert c.post("/api/stats/reset", json={"page": "nope"}).status_code == 400


def test_aprs_stations_since_counts_only_later_packets(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from rxapp import server
    d = tmp_path / "20261126-100000"
    d.mkdir()
    (d / "stations.json").write_text(json.dumps({"t": 300, "packets": 3, "stations": [
        {"name": "K6ABC", "last": 250, "packets": 2}, {"name": "W6OLD", "last": 50, "packets": 1}]}))
    (d / "packets.jsonl").write_text("".join(json.dumps(p) + "\n" for p in
                                             [{"t": 50, "from": "W6OLD"}, {"t": 60, "from": "K6ABC"}, {"t": 250, "from": "K6ABC"}]))
    monkeypatch.setattr(server, "APRS_DIR", tmp_path)
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    assert c.get(f"/api/aprs/stations?session={d.name}").json()["packets"] == 3
    r = c.get(f"/api/aprs/stations?session={d.name}&since=100").json()
    assert r["packets"] == 1 and [(s["name"], s["packets"]) for s in r["stations"]] == [("K6ABC", 1)]


def test_p25_talkgroups_carry_the_hours_call_times():
    from rxapp.p25 import Trunk
    tr = Trunk()
    tr._grant(1201, 0, 1000.0)
    tr._grant(1201, 0, 1002.0, update=True)        # the same call
    tr._grant(1201, 0, 1100.0)
    g = tr.snapshot(1101.0)["talkgroups"][0]
    assert g["grants"] == 2 and g["times"] == [1000.0, 1100.0]


# ---- P25 CQPSK front end (rxapp/p25.py CqpskDemod) -----------------------------------------------
def _cqpsk(dibit_steps, rate, fs=48000, snr_db=None, seed=0):
    """pi/4-DQPSK baseband: each symbol turns the phase by one of +-pi/4, +-3pi/4, RRC-shaped."""
    from rxapp.p25 import rrc_taps
    sps = fs // rate
    ph = np.cumsum(np.asarray(dibit_steps) * np.pi / 4)
    up = np.zeros(len(ph) * sps, complex)
    up[::sps] = np.exp(1j * ph)
    x = np.convolve(up, rrc_taps(sps), mode="same")
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        p = np.mean(np.abs(x) ** 2)
        x = x + np.sqrt(p / 10 ** (snr_db / 10) / 2) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    return x.astype(np.complex64)


@pytest.mark.parametrize("rate", [4800, 6000])
def test_cqpsk_demod_gives_c4fm_levels_at_symbol_centres(rate):
    from rxapp.p25 import CqpskDemod
    rng = np.random.default_rng(1)
    steps = rng.choice([-3, -1, 1, 3], 2000)
    iq = _cqpsk(steps, rate, snr_db=20)
    dm = CqpskDemod()
    a = np.concatenate([dm.process(iq[i:i + 960], rate) for i in range(0, len(iq), 960)]).astype(float)   # 20 ms packets
    sps = 48000 // rate
    best = 0
    for off in range(sps):                                   # the filter delay: find the sampling phase
        got = np.round(a[off::sps][:len(steps)] / CqpskDemod.SCALE)
        n = min(len(got), len(steps))
        for lag in range(0, 40):
            if n - lag > 1000:
                best = max(best, np.mean(got[lag:n] == steps[:n - lag]))
    assert best > 0.97


def test_p25_labels_calls_from_the_voice_channels_encryption_flag(tmp_path):
    from rxapp.p25 import Trunk, list_calls
    tr = Trunk()
    tr.feed(" VCH 1 - TG: 1451; SRC: 13180001;  Encrypted Group Voice - Abbreviated", 100.0)
    tr.feed(" VCH 0 - TG: 2351; SRC: 13180004;  Group Voice - Abbreviated", 101.0)
    tr.feed(" VCH 0 - ALG ID: 0x84 KEY ID: 0xA100 MI: 0x11FB6307BAC9E4B7 ESSB", 110.0)    # stale slot: ignored
    snap = tr.snapshot(111.0)
    assert snap["enc_tgs"] == [1451] and tr.clear_tgs == {2351}
    calls = tmp_path / "calls"
    calls.mkdir()
    for name in ("20261126_190526_1_P25_BEE005E45E1_1_GROUP_TGT_1451_SRC_13180001.wav",
                 "20261126_190358_1_P25_BEE005E45E1_1_GROUP_TGT_2351_SRC_13180004.wav"):
        (calls / name).write_bytes(b"\0" * 1000)
    got = {c["tg"]: c["enc"] for c in list_calls(calls, now=time.time() + 10, enc_calls=snap["enc_calls"],
                                                    enc_tgs=set(snap["enc_tgs"]))}
    assert got == {1451: True, 2351: False}


# ---- P25 live listening and transcripts (rxapp/p25live.py) ---------------------------------------
def _stereo(left, right, n=320):
    a = np.zeros((n, 2), np.int16)
    a[:, 0] = left
    a[:, 1] = right
    return a


def test_p25_follow_stays_with_the_talking_slot_then_moves():
    from rxapp.p25live import Follower
    f = Follower(None, lambda: {})
    assert f.pick(_stereo(1000, 1000), 0.0)[0] == 1000              # one voice on both sides: play it
    assert f.cur == 0 and f.pick(_stereo(1000, 2000), 0.1)[0] == 1000   # two voices: stay with slot 1
    assert f.pick(_stereo(0, 2000), 0.5)[0] == 0                     # slot 1 pauses briefly: keep waiting
    assert f.pick(_stereo(0, 2000), 1.2)[0] == 2000 and f.cur == 1   # quiet 0.8 s: move to slot 2


def test_p25_talkgroup_listener_plays_only_its_talkgroup():
    from rxapp.p25live import Follower
    slots = {"0": {"tg": 2451, "age": 0.5}, "1": {"tg": 2801, "age": 2.0}}
    f = Follower(2801, lambda: slots)
    assert f.pick(_stereo(1000, 2000), 0)[0] == 2000                 # slot 2 is TG 2801
    assert f.pick(_stereo(1000, 1000), 0) is None                    # one voice: slot 1 (named last) talks
    assert Follower(1234, lambda: slots).pick(_stereo(1000, 2000), 0) is None


def test_p25_calls_api_adds_transcripts(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from rxapp import server
    d = tmp_path / "20261126-200000"
    (d / "calls").mkdir(parents=True)
    names = ["20261126_200101_1_P25_BEE005E45E1_1_GROUP_TGT_2451_SRC_13274234.wav",
             "20261126_200201_1_P25_BEE005E45E1_1_GROUP_TGT_2801_SRC_13349322.wav"]
    for n in names:
        (d / "calls" / n).write_bytes(b"\0" * 16044)
        os.utime(d / "calls" / n, (time.time() - 60, time.time() - 60))
    (d / "transcripts.jsonl").write_text(json.dumps({"clip": names[0], "text": "Engine 51 medical aid", "text_atc": ""}) + "\n")
    monkeypatch.setattr(server, "P25_DIR", tmp_path)
    got = {c["tg"]: c for c in TestClient(server.app, client=("127.0.0.1", 50000)).get(f"/api/p25/calls?session={d.name}").json()}
    assert got[2451]["text"] == "Engine 51 medical aid" and got[2451]["transcribed"]
    assert "transcribed" not in got[2801]


def test_p25_other_slots_alg_continuation_doesnt_mark_a_clear_talkgroup():
    from rxapp.p25 import Trunk
    tr = Trunk()
    for i, line in enumerate([
            " VCH 0 - TG: 4773; SRC: 13425301;  Group Voice - Abbreviated",
            "        P25p2 LCH 0  MAC_PTT",
            " VCH 0 - TG 4773 SRC 13425301 ",
            " VCH 1 - TG: 1651; SRC: 13307879;  Encrypted Group Voice - Abbreviated",
            " VCH 1 - ALG ID: 0x84 KEY ID: 0xA100 MI: 0x3B4158C32A745EE6 ESSB",
            "      ALG ID: 0x84 KEY ID: 0xA100 MI(128): 0x3B4158C32A745EE64AA60AE020178BE7",
            "        P25p2 LCH 1  MAC_PTT",
            " VCH 1 - TG 1651 SRC 13307739 ",
            "        ALG ID: 0x84 KEY ID: 0xA100 MI: 0x0B8DB825F77C12E5 MPTT"]):
        tr.feed(line, 100.0 + i * 0.1)
    assert {tg for _, tg in tr.enc_calls} == {1651} and tr.snapshot(101)["enc_tgs"] == [1651]


def test_panels_describe_reports_the_range_a_scan_is_capturing():
    from rxapp.panels import PanelHub
    radio = SimpleNamespace(band="hf", vhf=SimpleNamespace(center_hz=None))
    hub = PanelHub.__new__(PanelHub)
    hub.radio, hub.hf, hub._next = radio, {}, 1
    hub.scan_state = lambda: None
    assert hub.describe([])["scan"] is None
    st = {"band_lo": 88.1e6, "band_hi": 107.9e6, "lo": 92e6, "hi": 96.6e6}
    hub.scan_state = lambda: st
    assert hub.describe([])["scan"] == st


def test_scanner_state_is_the_band_and_the_window_being_captured():
    from rxapp.jobs import Scanner
    sc = Scanner.__new__(Scanner)
    sc.active = sc.band_range = None
    assert sc.state() is None
    sc.band_range, sc.active = (88.1e6, 107.9e6), (92e6, 96.6e6)
    assert sc.state() == {"band_lo": 88.1e6, "band_hi": 107.9e6, "lo": 92e6, "hi": 96.6e6}
    sc.active = None                                  # between two windows: still scanning the band
    assert sc.state() == {"band_lo": 88.1e6, "band_hi": 107.9e6, "lo": None, "hi": None}


# ---- live UHF survey (rxapp/jobs.py Scanner.survey) ----------------------------------------------
def test_survey_steps_the_tuner_and_reports_100khz_cells(monkeypatch):
    import asyncio
    from rxapp.jobs import Scanner
    moves, seen = [], []
    vhf = SimpleNamespace(center_hz=773.45e6, if_hz=10e6)
    vhf.to_rf = lambda f: f - vhf.if_hz + vhf.center_hz            # (the real tuner inverts; the cells don't care)

    async def move(c):
        moves.append(c)
        vhf.center_hz = c
    radio = SimpleNamespace(band="vhf", vhf=vhf, move_tuner=move)
    sc = Scanner(SimpleNamespace(radio=radio, bins=2048, sweeping=False), None)

    async def capture(center, bin_hz, secs):
        f = center + (np.arange(2048) - 1024) * bin_hz
        a = np.full(2048, -120.0)
        a[np.argmin(np.abs(f - vhf.if_hz + vhf.center_hz - 462.5625e6))] = -95    # one carrier, +25 dB
        seen.append(sc.state())
        return f, a, a + 3
    monkeypatch.setattr(sc, "_capture_peak", capture)
    parts = []
    r = asyncio.run(sc.survey(450e6, 470e6, 0.5, on_window=parts.append))
    assert len(r["av"]) == 200 and r["windows"] == len(parts) == 3
    i = int((462.5625e6 - 450e6) // 100e3)
    assert r["av"][i] == 25.0 and r["mx"][i] == 28.0 and max(v for v in r["av"] if v is not None) == 25.0
    assert seen[0]["band_lo"] == 450e6 and seen[0]["lo"] is not None             # the tiles see the survey
    assert moves[-1] == 773.45e6 and sc.state() is None                          # the tuner back home


def test_survey_needs_the_voice_session_paused(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from rxapp import server
    f = tmp_path / "voice_control.json"
    f.write_text(json.dumps({"focus": "p25"}))
    monkeypatch.setattr(server, "FOCUS_FILE", f)
    assert TestClient(server.app, client=("127.0.0.1", 50000)).post("/api/survey/stream?lo_mhz=450&hi_mhz=470").status_code == 409


def _auth_app(tmp_path):
    from fastapi import FastAPI, WebSocket
    from rxapp.auth import Auth, AuthMiddleware
    app = FastAPI()

    @app.get("/api/x")
    def x():
        return {"ok": 1}

    @app.get("/page")
    def page():
        return {"page": 1}

    @app.websocket("/ws/x")
    async def wsx(ws: WebSocket):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()
    auth = Auth(tmp_path / "auth.json")
    app.add_middleware(AuthMiddleware, auth=auth)
    return app, auth


def test_auth_local_passes_remote_needs_login(tmp_path):
    from starlette.testclient import TestClient
    app, auth = _auth_app(tmp_path)
    local = TestClient(app, client=("127.0.0.1", 5000), base_url="https://testserver")
    assert local.get("/api/x").json() == {"ok": 1}                     # straight from the Mac: as before
    with local.websocket_connect("/ws/x") as ws:
        assert ws.receive_text() == "hi"
    fwd = {"X-Forwarded-For": "203.0.113.9"}                           # through Tailscale Funnel / serve
    assert local.get("/api/x", headers=fwd).status_code == 401
    r = local.get("/page", headers=fwd, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fpage")
    assert TestClient(app, client=("10.0.0.5", 1)).get("/api/x").status_code == 401
    # no password set: login can't succeed
    r = local.post("/login", headers=fwd, data={"password": "anything-at-all"}, follow_redirects=False)
    assert r.status_code == 200 and "No password is set" in r.text
    auth.set_password("correct horse battery")
    assert "Wrong password" in local.post("/login", headers=fwd, data={"password": "nope"}).text
    r = local.post("/login", headers=fwd, data={"password": "correct horse battery", "next": "/page"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/page"
    assert "HttpOnly" in r.headers["set-cookie"] and "Secure" in r.headers["set-cookie"]
    assert local.get("/api/x", headers=fwd).json() == {"ok": 1}
    ck = {"Cookie": "rxapp_session=" + local.cookies["rxapp_session"]}  # the test client's ws:// skips Secure cookies
    with local.websocket_connect("/ws/x", headers={**fwd, **ck}) as ws:
        assert ws.receive_text() == "hi"
    # off-site redirects are refused; a new password signs everyone out
    r = local.post("/login", headers=fwd, data={"password": "correct horse battery", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    auth.set_password("another long password")
    assert local.get("/api/x", headers=fwd).status_code == 401


def test_auth_websocket_refused_without_login(tmp_path):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    app, _ = _auth_app(tmp_path)
    c = TestClient(app, client=("127.0.0.1", 5000))
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/x", headers={"Tailscale-User-Login": "x"}) as ws:
            ws.receive_text()


def test_auth_rate_limits_guesses(tmp_path):
    from rxapp.auth import FAIL_MAX, Auth
    a = Auth(tmp_path / "auth.json")
    a.set_password("the real password")
    for _ in range(FAIL_MAX):
        assert not a.check_password("guess")
    assert not a.check_password("the real password")                 # locked for the window, even when right


def test_new_channel_switches_agc_on_after_the_seed_gain():
    """radiod treats a gain command as manual gain (AGC off): the seed must come before the AGC is switched on,
    or the channel stays at the seed (AM broadcast stuck at 100 dB, every sample clipped)."""
    import asyncio
    from rxapp.radio import Radio
    calls = []

    class Ctl:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append(name)
    r = Radio()
    r.ctl = Ctl()
    ssb = Channel(1, 7.2e6, "lsb", gain_db=95.0)
    am = Channel(2, 810e3, "am")
    r.channels = {1: ssb, 2: am}
    asyncio.run(r._open(am))
    assert "set_gain" in calls and "set_agc" in calls
    assert calls.index("set_gain") < calls.index("set_agc")
    assert calls[-1] != "set_gain"


def test_every_voice_session_service_may_make_channels():
    """voice_session.py makes its channels with kind = the profile's service; rxapp refused "uhf" (and "aprs"),
    so the UHF radio tab never got a channel."""
    import voice_session
    from rxapp.server import CHANNEL_KINDS
    for prof in (voice_session.AIR, voice_session.SAFETY, voice_session.MARINE, voice_session.APRS,
                 voice_session.UHF, voice_session.P25):
        assert prof.service in CHANNEL_KINDS, prof.service


def test_each_survey_page_keeps_its_own_last_survey(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from rxapp import server
    monkeypatch.setattr(server, "SURVEY_LAST", tmp_path / "survey_last.json")
    (tmp_path / "survey_last.json").write_text(json.dumps({"range_mhz": [200, 1000]}))
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    assert c.get("/api/survey/last").json()["range_mhz"] == [200, 1000]
    assert c.get("/api/survey/last?page=lband").json() is None
    (tmp_path / "survey_last_lband.json").write_text(json.dumps({"range_mhz": [1000, 1700]}))
    assert c.get("/api/survey/last?page=lband").json()["range_mhz"] == [1000, 1700]
    assert c.get("/api/survey/last?page=../x").status_code == 404
