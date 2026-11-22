# SPDX-License-Identifier: GPL-3.0-or-later
"""Fine-tune an SSB receiver from the voice it hears.

A mistuned SSB receiver shifts every frequency of the voice by the same amount, so the harmonics of a voice with
pitch f0 land at k*f0 + d instead of k*f0. The pitch itself comes from the envelope (the pulse rate of the voice),
which the shift doesn't touch. For every voiced 40 ms frame the spectrum is scored against the comb k*f0 + d for
every candidate d; summed over many frames (the pitch moves, so a wrong d that fits one frame's comb fails the
others), the best d is the tuning error. A few seconds of speech give it within ~5 Hz; under ~30 voiced frames
(1.2 s) it can be far off, so estimates are pooled until MIN_FRAMES.

    d > 0: the voice is too high. USB: the dial is d too low; LSB: d too high.

Amateur nets sit on whole or half kHz, so a corrected dial within SNAP_HZ of one is put on it.
"""
from typing import Optional, Tuple

import numpy as np

D = np.arange(-400, 401, 2.0)        # candidate offsets (Hz)
FRAME_SEC = 0.04
VOICED_R = 0.55                      # envelope pitch autocorrelation that counts as voiced (as speech.py)
MIN_FRAMES = 40                      # voiced frames pooled before a decision (~1.6 s of voice)
SNAP_HZ = 30                         # snap to a whole / half kHz this close
MIN_MOVE_HZ = 25                     # closer than this is in tune: leave it
GRID_HZ = 500


def comb_scores(a: np.ndarray, rate: int) -> Tuple[np.ndarray, int]:
    """Per-offset comb scores summed over the voiced frames of a, and how many frames were voiced."""
    from scipy.signal import butter, hilbert, sosfiltfilt
    a = np.asarray(a, float)
    score = np.zeros(len(D))
    if len(a) < rate * 0.3:
        return score, 0
    x = sosfiltfilt(butter(4, [150, 3200], "band", fs=rate, output="sos"), a)
    e = sosfiltfilt(butter(4, 400, "low", fs=rate, output="sos"), np.abs(hilbert(x)))
    L = int(rate * FRAME_SEC)
    N = 8192
    fr = np.fft.rfftfreq(N, 1 / rate)
    win = np.hanning(L)
    lo, hi = int(rate / 320), int(rate / 70)
    w = (L - np.arange(L)) / L
    used = 0
    for i in range(len(x) // L):
        seg = e[i * L:(i + 1) * L] - e[i * L:(i + 1) * L].mean()
        ac = np.fft.irfft(np.abs(np.fft.rfft(seg, 2 * L)) ** 2, 2 * L)[:L]
        r = ac[lo:hi] / w[lo:hi] / max(ac[0], 1e-9)
        j = int(np.argmax(r))
        if r[j] < VOICED_R:
            continue
        f0 = rate / (lo + j)
        S = np.abs(np.fft.rfft(x[i * L:(i + 1) * L] * win, N))
        S = S / (S.sum() + 1e-12)
        k = np.arange(1, int(3000 / f0) + 1)
        f = k[None, :] * f0 + D[:, None]
        score += (np.interp(f, fr, S) * ((f > 250) & (f < 2900))).sum(1)
        used += 1
    return score, used


def offset(score: np.ndarray) -> float:
    return float(D[int(np.argmax(score))])


def estimate(a: np.ndarray, rate: int) -> Tuple[Optional[float], int]:
    s, n = comb_scores(a, rate)
    return (offset(s) if n else None), n


def corrected_dial(dial_hz: float, mode: str, d_hz: float) -> Tuple[float, bool]:
    """The dial that puts the voice where it belongs, and whether it was snapped to the half-kHz grid."""
    target = dial_hz + d_hz if mode == "usb" else dial_hz - d_hz
    grid = round(target / GRID_HZ) * GRID_HZ
    if abs(grid - target) <= SNAP_HZ:
        return float(grid), True
    return float(round(target / 10) * 10), False


class AutoTune:
    """Pools estimates for one dial setting and decides when (and where) to move it.

    The first move needs one pooled estimate; once it has moved, a further move needs two estimates in a row
    that agree, so a second station a little off the first doesn't make it hop back and forth.
    """
    AGREE_HZ = 20

    def __init__(self):
        self.reset(None)
        self.moves = 0
        self.last = None

    def reset(self, dial_hz: Optional[float]):
        self.dial = dial_hz
        self.score = np.zeros(len(D))
        self.frames = 0
        self.pending = None              # a move waiting for a second estimate

    def new_station(self, dial_hz: float):
        self.reset(dial_hz)
        self.moves = 0
        self.last = None

    def add(self, score: np.ndarray, frames: int, dial_hz: float, mode: str, now: float) -> Optional[float]:
        """One utterance heard at dial_hz. Returns the dial to move to, or None."""
        if self.dial is None or abs(dial_hz - self.dial) >= 1:
            self.reset(dial_hz)          # tuned elsewhere (by hand) since: start over there
        if not frames:
            return None
        self.score += score
        self.frames += frames
        if self.frames < MIN_FRAMES:
            return None
        d = offset(self.score)
        target, snapped = corrected_dial(dial_hz, mode, d)
        frames, self.score, self.frames = self.frames, np.zeros(len(D)), 0
        move, wait = abs(target - dial_hz) >= MIN_MOVE_HZ, False
        if not move:
            self.pending = None
        elif self.moves and (self.pending is None or abs(self.pending - target) > self.AGREE_HZ):
            self.pending, move, wait = target, False, True
        self.last = {"t": now, "offset_hz": d, "frames": frames, "from_hz": dial_hz, "to_hz": target,
                     "snapped": snapped, "moved": move, "waiting": wait}
        if not move:
            return None
        self.moves += 1
        self.reset(target)
        return target

    def status(self) -> dict:
        return {"frames": self.frames, "need": MIN_FRAMES, "moves": self.moves, "last": self.last}
