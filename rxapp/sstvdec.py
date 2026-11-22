# SPDX-License-Identifier: GPL-3.0-or-later
"""A noise-tolerant SSTV decoder for HF.

The `sstv` package decodes clean signals well but returns nothing once the picture drops to ~12 dB SNR, which
is where most HF pictures are. This decoder does what slowrx and MMSSTV do instead: it finds the mode and the
exact line rate from the 1200 Hz line-sync pulses over the whole capture (no VIS header needed, and a picture
already under way still decodes), then reads every line at its predicted time, so a noisy line becomes a noisy
row of pixels rather than a lost picture.

    1. band-limit to 1050-2350 Hz; instantaneous frequency from the analytic signal.
    2. sync likelihood: energy within +-75 Hz of 1200 Hz against energy in the picture band.
    3. for each mode, fold the sync likelihood at its line period, scanning the period +-0.4 % (the sender's
       and our sample clocks differ, which slants the picture): the best (mode, period, phase) has the most
       sync energy at one place in the line. That's also the confidence test.
    4. the picture spans the lines whose own sync matches; each pixel is the mean frequency over its time.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import scipy.fft as sfft
from PIL import Image

SYNC_HZ, BLACK_HZ, WHITE_HZ = 1200.0, 1500.0, 2300.0


@dataclass(frozen=True)
class Mode:
    name: str
    family: str               # martin | scottie | robot36 | robot72 | pd
    width: int
    height: int
    period_ms: float          # sync to sync
    sync_ms: float
    scan_ms: float            # one colour component of one line (PD: one of Y0, R-Y, B-Y, Y1)

    @property
    def lines_per_sync(self):
        return 2 if self.family == "pd" else 1

    @property
    def syncs(self):
        return self.height // self.lines_per_sync

    def segments(self) -> List[Tuple[str, float]]:
        """(component, start ms after the sync pulse starts). Scottie sends G and B before its sync."""
        s = self.scan_ms
        if self.family == "martin":
            p = self.sync_ms + 0.572
            return [("G", p), ("B", p + s + 0.572), ("R", p + 2 * (s + 0.572))]
        if self.family == "scottie":
            return [("G", -(2 * s + 1.5)), ("B", -s), ("R", self.sync_ms + 1.5)]
        if self.family == "robot36":
            return [("Y", 12.0), ("C", 12.0 + s + 6.0)]
        if self.family == "robot72":
            return [("Y", 12.0), ("V", 12.0 + s + 6.0), ("U", 12.0 + 1.5 * s + 12.0)]
        p = self.sync_ms + 2.08
        return [("Y0", p), ("V", p + s), ("U", p + 2 * s), ("Y1", p + 3 * s)]

    def seg_ms(self, comp):
        return self.scan_ms / 2 if self.family == "robot72" and comp in ("U", "V") else \
            self.scan_ms / 2 if self.family == "robot36" and comp == "C" else self.scan_ms


MODES = [
    Mode("Martin M1", "martin", 320, 256, 446.446, 4.862, 146.432),
    Mode("Martin M2", "martin", 320, 256, 226.798, 4.862, 73.216),
    Mode("Scottie S1", "scottie", 320, 256, 428.22, 9.0, 138.24),
    Mode("Scottie S2", "scottie", 320, 256, 277.692, 9.0, 88.064),
    Mode("Scottie DX", "scottie", 320, 256, 1050.3, 9.0, 345.6),
    Mode("Robot 36", "robot36", 320, 240, 150.0, 9.0, 88.0),
    Mode("Robot 72", "robot72", 320, 240, 300.0, 9.0, 138.0),
    Mode("PD50", "pd", 320, 256, 388.16, 20.0, 91.52),
    Mode("PD90", "pd", 320, 256, 703.04, 20.0, 170.24),
    Mode("PD120", "pd", 640, 496, 508.48, 20.0, 121.6),
    Mode("PD160", "pd", 512, 400, 804.416, 20.0, 195.584),
    Mode("PD180", "pd", 640, 496, 754.24, 20.0, 183.04),
    Mode("PD240", "pd", 640, 496, 1000.0, 20.0, 244.48),
    Mode("PD290", "pd", 800, 616, 937.28, 20.0, 228.8),
]
BY_NAME = {m.name: m for m in MODES}

FOLD_RATE = 2000.0            # Hz: the sync likelihood is folded at 0.5 ms resolution
DRIFT = 0.004                 # +-0.4 % line-period search
MIN_CONTRAST = 4.0            # folded sync peak over the folded spread (noise alone: ~2.5)
MIN_LINES = 24                # syncs that must match for a picture
MAX_GAP = 16                  # lines without a fitting sync that still don't split a picture


def _bandpass(x, rate, lo, hi):
    n = len(x)
    m = sfft.next_fast_len(n, real=True)                 # an awkward length makes the FFT many times slower
    spec = sfft.rfft(x, m, workers=-1)
    f = np.fft.rfftfreq(m, 1 / rate)
    spec[(f < lo) | (f > hi)] = 0
    return sfft.irfft(spec, m, workers=-1)[:n]


def _analytic(x):
    n = len(x)
    m = sfft.next_fast_len(n)
    X = sfft.fft(x, m, workers=-1)
    h = np.zeros(m)
    h[0] = 1
    if m % 2 == 0:
        h[m // 2] = 1
        h[1:m // 2] = 2
    else:
        h[1:(m + 1) // 2] = 2
    return sfft.ifft(X * h, workers=-1)[:n]


def _smooth(x, n):
    if n <= 1:
        return x
    c = np.cumsum(np.concatenate([[0.0], x]))
    y = (c[n:] - c[:-n]) / n
    pad = n - 1
    return np.concatenate([np.full(pad // 2, y[0]), y, np.full(pad - pad // 2, y[-1])])


def frequency_track(audio, rate):
    """Instantaneous frequency (Hz) and a sync likelihood (0..1) per sample."""
    x = _bandpass(np.asarray(audio, np.float64), rate, 1050.0, 2350.0)
    z = _analytic(x)
    dphi = np.angle(z[1:] * np.conj(z[:-1]))
    freq = np.concatenate([[dphi[0]], dphi]) * rate / (2 * np.pi)
    # sync likelihood from narrow-band energies, which hold up in noise far better than the frequency itself
    e_sync = np.abs(_analytic(_bandpass(x, rate, SYNC_HZ - 75, SYNC_HZ + 75))) ** 2
    e_vid = np.abs(_analytic(_bandpass(x, rate, BLACK_HZ - 50, WHITE_HZ + 50))) ** 2
    k = max(1, int(0.002 * rate))
    e_sync, e_vid = _smooth(e_sync, k), _smooth(e_vid, k)
    like = e_sync / (e_sync + e_vid / 4 + 1e-12)        # /4: the video band is ~5x wider than the sync band
    return freq, like


def _fold(like_ds, period_s, rate):
    """Mean sync likelihood by position in the line."""
    n = int(round(period_s * rate))
    pos = (np.arange(len(like_ds)) / rate) % period_s
    idx = np.minimum((pos / period_s * n).astype(int), n - 1)
    return np.bincount(idx, like_ds, n) / np.maximum(np.bincount(idx, None, n), 1)


def _peak(ac, lo, hi):
    """Highest autocorrelation in lags lo..hi, with its lag refined by a parabola."""
    lo, hi = max(1, int(lo)), min(len(ac) - 2, int(np.ceil(hi)))
    if hi <= lo:
        return 0.0, float(lo)
    i = lo + int(np.argmax(ac[lo:hi + 1]))
    a, b, c = ac[i - 1], ac[i], ac[i + 1]
    d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) < 0 else 0.0
    return float(b), i + float(np.clip(d, -0.5, 0.5))


def find_mode(like, rate, modes=MODES) -> Optional[dict]:
    """The mode and its exact line period from the autocorrelation of the sync likelihood: lines repeat, so
    it peaks at the line period. A mode whose period is a multiple of another's (Robot 72 = 2 x Robot 36,
    Scottie DX = 7 x Robot 36) peaks too, so the shortest period that explains the peak wins. The period is
    then refined at multiples of itself (k lines apart the peak is k times as precise), and the phase found by
    folding at that period."""
    step = max(1, int(round(rate / FOLD_RATE)))
    ds = like[: len(like) // step * step].reshape(-1, step).mean(1)
    fr = rate / step
    x = ds - ds.mean()
    nfft = 1 << int(np.ceil(np.log2(2 * len(x))))
    X = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(np.abs(X) ** 2, nfft)[: len(x)]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    span = len(ds)
    scores = {}
    for m in modes:
        P = m.period_ms / 1000 * fr
        if span < 12 * P:
            continue
        scores[m.name] = _peak(ac, P * (1 - DRIFT) - 1, P * (1 + DRIFT) + 1)
    if not scores:
        return None
    best = max(scores, key=lambda k: scores[k][0])
    for name in sorted(scores, key=lambda k: BY_NAME[k].period_ms):     # shortest period first
        r = BY_NAME[best].period_ms / BY_NAME[name].period_ms
        n = round(r)
        if n >= 2 and abs(r - n) / n < DRIFT and scores[name][0] >= 0.6 * scores[best][0]:
            best = name
            break
    m = BY_NAME[best]
    P = scores[best][1]
    k = 2
    while k * P < span / 2 and k <= 64:
        _, lag = _peak(ac, k * P - max(2, 0.002 * k * P), k * P + max(2, 0.002 * k * P))
        P = lag / k
        k *= 2
    period = P / fr
    prof = _fold(ds, period, fr)
    w = max(1, int(round(m.sync_ms / 1000 * fr)))
    box = np.convolve(np.concatenate([prof, prof[:w]]), np.ones(w) / w, "valid")[: len(prof)]
    i = int(np.argmax(box))
    contrast = (box[i] - np.median(box)) / (np.std(box) + 1e-9)
    return {"mode": m, "period": period, "phase": i / fr, "contrast": float(contrast), "ac": scores[best][0]}


def _line_starts(like, rate, found):
    """The picture's sync pulses: predicted every period from the fold's phase, each one measured where its
    own sync energy starts (within +-3 ms), then a robust straight-line fit through the measured times gives
    every line's start (a noisy or missing pulse doesn't move its line). The picture is the longest run of
    lines whose own sync stands out."""
    m, T = found["mode"], found["period"]
    w = max(1, int(round(m.sync_ms / 1000 * rate)))
    c = np.concatenate([[0.0], np.cumsum(like)])
    t = np.arange(found["phase"], len(like) / rate - T, T)
    if not len(t):
        return t
    sh = int(0.003 * rate)
    meas, score = [], []
    for s in t:
        i0 = int(round(s * rate))
        lo, hi = max(0, i0 - sh), min(len(like) - w, i0 + sh)
        if hi <= lo:
            meas.append(np.nan); score.append(0.0); continue
        boxes = (c[lo + w:hi + w + 1] - c[lo:hi + 1]) / w
        j = int(np.argmax(boxes))
        meas.append((lo + j) / rate); score.append(boxes[j])
    meas, score = np.array(meas), np.array(score)
    # a picture line: sync energy well above the rest of its own line (video), which noise doesn't have
    P = int(round(T * rate))
    rest = np.array([np.median(like[int(s * rate): int(s * rate) + P]) if int(s * rate) + P <= len(like) else 1.0 for s in t])
    contrast = score - rest
    good = (contrast > 0.5 * np.percentile(contrast, 90)) & (contrast > 0.15) & np.isfinite(meas)
    k = np.arange(len(t))
    if good.sum() < 4:
        return t[:0]
    # the line fit: through the good pulses, dropping the worst ones twice
    sel = good.copy()
    slope, icpt = T, found["phase"]
    for _ in range(3):
        A = np.vstack([k[sel], np.ones(sel.sum())]).T
        (slope, icpt), *_ = np.linalg.lstsq(A, meas[sel], rcond=None)
        res = np.abs(meas - (slope * k + icpt))
        sel = good & (res <= max(0.0005, 3 * np.median(res[sel])))
        if sel.sum() < 4:
            return t[:0]
    res = np.abs(meas - (slope * k + icpt))
    # picture lines: a sync on the fitted line (noise puts its best spot anywhere in the +-3 ms search)
    fits = good & (res <= 0.001)
    # the picture: the cluster with the most fitting lines, where gaps of up to MAX_GAP lines (a fade, a burst
    # of noise) don't split it; noise puts a line on the fit only by chance (~1 in 3), never densely
    idx = np.flatnonzero(fits)
    if not len(idx):
        return t[:0]
    cuts = np.flatnonzero(np.diff(idx) > MAX_GAP) + 1
    clusters = np.split(idx, cuts)
    best = max(clusters, key=lambda c: len(c) / max(1, c[-1] - c[0] + 1) * len(c))
    if len(best) < 0.4 * (best[-1] - best[0] + 1):
        return t[:0]
    inside = best
    a0, b0 = inside[0], inside[-1] + 1                          # trim to lines that really fit
    if b0 - a0 >= m.syncs - 3:
        # (nearly) a whole picture: of the windows of exactly one picture's lines around the run, the one with
        # the most sync contrast. The first line's sync follows the VIS stop bit (1200 Hz too), which pulls its
        # measured start off the fit, and a noise line just after the picture can fit by chance.
        c = np.clip(np.where(np.isfinite(contrast), contrast, 0), 0, None)
        lo_s = max(0, a0 - 3)
        hi_s = max(lo_s, min(len(t), b0 + 3) - m.syncs)
        a0 = max(range(lo_s, hi_s + 1), key=lambda st: c[st:st + m.syncs].sum())
        b0 = min(len(t), a0 + m.syncs)
    else:
        for _ in range(2):                                  # a partial picture: take back the first line
            if a0 > 0 and good[a0 - 1]:
                a0 -= 1
    t = slope * k + icpt
    return t[a0:b0][: m.syncs]


class _Track:
    """The frequency track with its running sum, so a pixel's mean frequency is two lookups."""
    def __init__(self, freq):
        self.n = len(freq)
        self.c = np.concatenate([[0.0], np.cumsum(freq)])


def _read(tr: "_Track", rate, t0, dur_ms, n):
    """n pixel values (0..255) from the mean frequency over each pixel's time."""
    a = t0 * rate
    edges = a + np.arange(n + 1) * (dur_ms / 1000 * rate / n)
    lo = np.clip(np.floor(edges[:-1]).astype(int), 0, tr.n - 1)
    hi = np.clip(np.maximum(np.floor(edges[1:]).astype(int), lo + 1), 1, tr.n)
    f = (tr.c[hi] - tr.c[lo]) / (hi - lo)
    return np.clip((f - BLACK_HZ) / (WHITE_HZ - BLACK_HZ) * 255, 0, 255)


def _ycc(Y, U, V):
    """YCbCr to RGB, full swing (JPEG / BT.601 full range): what the senders' software uses, measured against
    the `sstv` encoder (studio swing was off by a few percent in every channel)."""
    u, v = U - 128.0, V - 128.0
    return np.clip(np.stack([Y + 1.402 * v, Y - 0.344136 * u - 0.714136 * v, Y + 1.772 * u], -1), 0, 255)


def render(freq, rate, mode: Mode, starts) -> Image.Image:
    freq = _Track(freq)
    W, H = mode.width, mode.height
    rgb = np.zeros((H, W, 3))
    segs = dict(mode.segments())
    fam = mode.family
    if fam in ("martin", "scottie"):
        for k, s in enumerate(starts):
            for ch, j in (("R", 0), ("G", 1), ("B", 2)):
                rgb[k, :, j] = _read(freq, rate, s + segs[ch] / 1000, mode.seg_ms(ch), W)
    elif fam == "pd":
        for k, s in enumerate(starts):
            Y0 = _read(freq, rate, s + segs["Y0"] / 1000, mode.scan_ms, W)
            V = _read(freq, rate, s + segs["V"] / 1000, mode.scan_ms, W)
            U = _read(freq, rate, s + segs["U"] / 1000, mode.scan_ms, W)
            Y1 = _read(freq, rate, s + segs["Y1"] / 1000, mode.scan_ms, W)
            if 2 * k + 1 < H:
                rgb[2 * k], rgb[2 * k + 1] = _ycc(Y0, U, V), _ycc(Y1, U, V)
    elif fam == "robot72":
        for k, s in enumerate(starts):
            Y = _read(freq, rate, s + segs["Y"] / 1000, mode.scan_ms, W)
            V = _read(freq, rate, s + segs["V"] / 1000, mode.seg_ms("V"), W)
            U = _read(freq, rate, s + segs["U"] / 1000, mode.seg_ms("U"), W)
            rgb[k] = _ycc(Y, U, V)
    else:                                                 # Robot 36: R-Y on even lines, B-Y on odd, shared
        Ys, Cs = [], []
        for s in starts:
            Ys.append(_read(freq, rate, s + segs["Y"] / 1000, mode.scan_ms, W))
            Cs.append(_read(freq, rate, s + segs["C"] / 1000, mode.seg_ms("C"), W))
        for k in range(len(starts)):
            # which chroma a line carries: its separator tone (1500 Hz = R-Y, 2300 Hz = B-Y)
            sep = _read(freq, rate, starts[k] + (12.0 + mode.scan_ms) / 1000, 4.5, 1)[0]
            is_v = sep < 128
            j = k + 1 if is_v else k - 1                      # pairs: (R-Y line, B-Y line)
            j = min(max(j, 0), len(starts) - 1)
            V, U = (Cs[k], Cs[j]) if is_v else (Cs[j], Cs[k])
            rgb[k] = _ycc(Ys[k], U, V)
    return Image.fromarray(rgb.astype(np.uint8), "RGB")


def decode(audio, rate) -> List[Image.Image]:
    """Pictures found in the audio (at most one: the strongest). Empty when there's no SSTV line rate in it."""
    audio = np.asarray(audio, np.float64)
    if len(audio) < rate * 5:
        return []
    freq, like = frequency_track(audio, rate)
    found = find_mode(like, rate)
    if not found or found["contrast"] < MIN_CONTRAST:
        return []
    starts = _line_starts(like, rate, found)
    if len(starts) < MIN_LINES:
        return []
    m = found["mode"]
    im = render(freq, rate, m, starts)
    im.info["sstv_mode"] = m.name
    im.info["sstv_complete"] = len(starts) >= m.syncs
    im.info["sstv_lines"] = len(starts) * m.lines_per_sync
    im.info["sstv_contrast"] = round(found["contrast"], 1)
    return [im]
