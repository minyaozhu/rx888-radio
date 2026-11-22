# SPDX-License-Identifier: GPL-3.0-or-later
"""Speech to text for the receiver's voice pages, shared by voice_session.py (airband, public safety, UHF,
marine) and rxapp's SSB transcriber: Whisper on-device (mlx), with what keeps it from inventing words out of
radio noise: a voicing test before it runs, its repetition limit, and stock phrases it makes up."""
import math
import re

import numpy as np

WHISPER = "mlx-community/whisper-large-v3-turbo"

HALLUCINATIONS = {"", "thank you.", "thanks for watching!", "you", "bye.", ".", "thank you very much.",
                  # what Whisper invented most often from public-safety noise (Nov 2026, 41.1025 MHz above all):
                  # stock phrases nobody sends alone on a dispatch channel
                  "i'm sorry.", "i don't know.", "i'm going to go.", "i'm out.", "amen.", "peace.", "so", "but",
                  "thank you. thank you.", "i'll see you next time.", "ah!", "oh, my god.", "i'll be right back."}
VOICED_MIN = 0.08            # share of voiced 30 ms frames below which a clip isn't speech (see voiced_share)
COMPRESSION_MAX = 2.4        # Whisper's own repetition limit: "Yeah. Yeah. Yeah..." scores 5.6, real speech <= 1.7


def prepare(a: np.ndarray, rate: int = 12000) -> np.ndarray:
    """Demodulated audio -> what Whisper hears best: DC removed (an AM carrier's level), voice band only,
    normalised, 16 kHz. (Fed raw AM, Whisper called nearly every transmission 'no speech'.)"""
    from scipy.signal import butter, resample_poly, sosfilt
    x = a.astype(np.float32) / 32768
    x = sosfilt(butter(4, [250, 3200], btype="band", fs=rate, output="sos"), x - x.mean())
    x = x / (np.abs(x).max() + 1e-9) * 0.8
    g = math.gcd(16000, int(rate))
    return resample_poly(x, 16000 // g, int(rate) // g).astype(np.float32)


FRAME_SEC = 0.03             # voicing is judged per 30 ms frame


def voiced_frames(a: np.ndarray, rate: int) -> np.ndarray:
    """Per 30 ms frame: does it carry a pitched voice? The 300-3000 Hz band's normalized autocorrelation
    peaks above 0.45 at an 80-300 Hz pitch lag. Speech has it in vowels; FM noise, SSB hiss and static don't."""
    from scipy.signal import butter, sosfiltfilt
    L = int(rate * FRAME_SEC)
    if len(a) < 2 * L:
        return np.zeros(0, bool)
    x = sosfiltfilt(butter(4, [300, 3000], "band", fs=rate, output="sos"), np.asarray(a, np.float64))
    fr = x[: len(x) // L * L].reshape(-1, L)
    fr = fr - fr.mean(1, keepdims=True)
    den = (fr * fr).sum(1)
    F = np.fft.rfft(fr, 2 * L)
    ac = np.fft.irfft(np.abs(F) ** 2, 2 * L)[:, :L]
    pk = ac[:, int(rate / 300):int(rate / 80)].max(1) / np.maximum(den, 1e-9)
    return pk > 0.45


ENV_FRAME_SEC = 0.04         # envelope pitch frames (SSB)
ENV_THRESHOLD = 0.55


def envelope_voiced_frames(a: np.ndarray, rate: int) -> np.ndarray:
    """Per 40 ms frame: does the 300-3000 Hz envelope pulse at a voice pitch (70-320 Hz)? For SSB, where a
    mistuned receiver shifts every harmonic by the same amount: the waveform's pitch is then lost (60 Hz off a
    120 Hz voice read 0.00 with voiced_frames) but the envelope still pulses at the pitch. Calibrated on
    synthetic voice in 300-2700 Hz noise: hiss passes 1.8% of frames (a false 6-of-20 start ~0.1/hour);
    voice with syllable gaps passes 61% at 10 dB SNR, 49% at 5 dB, 25% at 3 dB, at any mistuning."""
    from scipy.signal import butter, hilbert, sosfiltfilt
    L = int(rate * ENV_FRAME_SEC)
    if len(a) < 2 * L:
        return np.zeros(0, bool)
    x = sosfiltfilt(butter(4, [300, 3000], "band", fs=rate, output="sos"), np.asarray(a, np.float64))
    e = sosfiltfilt(butter(4, 400, "low", fs=rate, output="sos"), np.abs(hilbert(x)))
    fr = e[: len(e) // L * L].reshape(-1, L)
    fr = fr - fr.mean(1, keepdims=True)
    den = (fr * fr).sum(1)
    F = np.fft.rfft(fr, 2 * L)
    ac = np.fft.irfft(np.abs(F) ** 2, 2 * L)[:, :L]
    lo, hi = int(rate / 320), int(rate / 70)
    w = (L - np.arange(L)) / L                  # unbias: fewer samples overlap at longer lags
    return (ac[:, lo:hi] / w[lo:hi]).max(1) / np.maximum(den, 1e-9) > ENV_THRESHOLD


def voiced_share(a: np.ndarray, rate: int) -> float:
    """Share of 30 ms frames carrying a pitched voice (voiced_frames). Measured on public-safety clips: real
    speech (150 clips with real text) 0.07-0.5, only 2% below 0.08; 41.1025 MHz noise, where Whisper invented
    "I'm sorry." and "Okay." hundreds of times, 90% below 0.08."""
    if len(a) < rate * 0.3:
        return 0.0
    v = voiced_frames(a, rate)
    return float(v.mean()) if len(v) else 0.0


def transcribe(a: np.ndarray, rate: int = 12000, prompt: str = None) -> dict:
    """Two readings: plain, and with the service's vocabulary prompt (it helps with call signs and
    phrases, but can also pull a garbled transmission towards stock phrases)."""
    import mlx_whisper
    x = prepare(a, rate)
    out = {}
    for key, pr in (("text", None), ("text_atc", prompt)):
        r = mlx_whisper.transcribe(x, path_or_hf_repo=WHISPER, language="en", initial_prompt=pr,
                                   condition_on_previous_text=False, temperature=0.0)
        segs = r.get("segments") or []
        text = " ".join(s["text"].strip() for s in segs if s.get("no_speech_prob", 0) < 0.8
                        and s.get("compression_ratio", 0) <= COMPRESSION_MAX).strip()
        caption = re.fullmatch(r"[\[(]?[A-Z][A-Z ,.'!-]*[\])]?", text) is not None   # "BELL RINGS": a sound caption
        out[key] = "" if text.lower() in HALLUCINATIONS or caption else text
    return out
