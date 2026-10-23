"""Mode S / ADS-B (1090 MHz) demodulator for 2 MS/s magnitude samples.

The classic dump1090 approach at 2 samples per microsecond: an 8 us preamble with pulses at 0, 1,
3.5 and 4.5 us (samples 0, 2, 7, 9), then 56 or 112 bits of pulse-position modulation, one bit per
microsecond: a pulse in the first half is a 1, in the second half a 0. Candidates are found with
vectorised numpy comparisons; only those whose first five bits say DF 17/18 (ADS-B) or DF 11
(all-call reply) are checked further, and pyModeS validates the CRC and decodes them.
"""

import numpy as np

FS = 2_000_000
PRE = 16                     # preamble length in samples (8 us)
LONG, SHORT = 112, 56


def preambles(m: np.ndarray) -> np.ndarray:
    """Sample indices where an ADS-B preamble starts."""
    n = len(m) - PRE - 2 * LONG
    if n <= 0:
        return np.zeros(0, int)
    p = [m[k:k + n] for k in range(PRE)]
    hi = (p[0] + p[2] + p[7] + p[9]) / 4
    quiet = np.maximum.reduce([p[1], p[3], p[4], p[5], p[6], p[8], p[10], p[11], p[12], p[13], p[14], p[15]])
    ok = ((p[0] > p[1]) & (p[1] < p[2]) & (p[2] > p[3]) & (p[3] < p[0]) & (p[4] < p[0]) & (p[5] < p[0])
          & (p[6] < p[0]) & (p[7] > p[8]) & (p[8] < p[9]) & (p[9] > p[6])
          & (quiet < hi * 0.65)                                   # the gaps really are gaps
          & (hi > np.median(m) * 2.5))                            # and the pulses stand above the noise
    return np.flatnonzero(ok)


def bits_at(m: np.ndarray, starts: np.ndarray, nbits: int = LONG) -> np.ndarray:
    """PPM bits after each preamble: 1 where the first half-microsecond is stronger."""
    idx = starts[:, None] + PRE + 2 * np.arange(nbits)[None, :]
    return (m[idx] > m[idx + 1]).astype(np.uint8)


def to_hex(bits: np.ndarray) -> str:
    return np.packbits(bits).tobytes().hex().upper()


def demodulate(iq: np.ndarray):
    """[(sample index, hex message)] for frames whose downlink format is worth decoding."""
    m = np.abs(iq).astype(np.float32)
    starts = preambles(m)
    if not len(starts):
        return []
    b = bits_at(m, starts)
    df = (b[:, 0] << 4) | (b[:, 1] << 3) | (b[:, 2] << 2) | (b[:, 3] << 1) | b[:, 4]
    out, last = [], -10 ** 9
    for i in np.flatnonzero((df == 17) | (df == 18) | (df == 11)):
        s = int(starts[i])
        if s - last < 2 * PRE:              # the same frame found again one sample later
            continue
        n = LONG if df[i] in (17, 18) else SHORT
        out.append((s, to_hex(b[i, :n])))
        last = s
    return out
