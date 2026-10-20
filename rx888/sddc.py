"""Talk to an RX-888 running the SDDC firmware: GPIOs, ADC, VHF tuner, sample stream.

Command codes and GPIO bits follow libsddc (SDR++ source_modules/sddc_source).
The ADC stream is real 16-bit samples on bulk endpoint 0x81.
"""

import struct

import numpy as np
import usb.core

from . import usb as rxusb

# Vendor requests
FX3_START, FX3_STOP, FX3_GPIO = 0xAA, 0xAB, 0xAD
SET_PARAM, ADC_SET_RATE = 0xB6, 0xB2
TUNER_START, TUNER_TUNE, TUNER_STOP = 0xB4, 0xB5, 0xB8

# GPIO bits
SEL0, SEL1, SHUTDOWN = 1 << 3, 1 << 4, 1 << 5
DITHER, RANDOM = 1 << 6, 1 << 7
VHF_EN, PGA_EN = 1 << 15, 1 << 16

# set_param ids
R82XX_ATT, R82XX_VGA, AD8340_VGA = 1, 2, 11

TUNER_REF_HZ = 16_000_000  # what libsddc passes to TUNER_START


class RX888:
    def __init__(self):
        state, dev = rxusb.find()
        if state != "sddc":
            raise RuntimeError(f"RX-888 not ready (state: {state}); load the firmware first")
        self.dev = dev
        dev.set_configuration()
        self.gpio = SHUTDOWN
        self.samplerate = None
        self._out(FX3_STOP, b"\0" * 4)

    def _out(self, req, data, value=0, index=0):
        self.dev.ctrl_transfer(0x40, req, value, index, data, timeout=1000)

    def set_gpio(self, bits, on):
        self.gpio = (self.gpio & ~bits) | (bits if on else 0)
        self._out(FX3_GPIO, struct.pack("<I", self.gpio))

    def set_param(self, param, value):
        self._out(SET_PARAM, b"\0" * 4, value=value, index=param)

    def hf_mode(self):
        """HF port, direct sampling, VHF tuner off."""
        self._out(TUNER_STOP, b"\0" * 4)
        self.set_gpio(VHF_EN | SEL1, False)
        self.set_gpio(SEL0, True)

    def vhf_mode(self, freq_hz, att=15, vga=9):
        """VHF/UHF port through the R828D tuner, tuned to freq_hz."""
        self.set_gpio(SEL0, False)
        self.set_gpio(SEL1 | VHF_EN, True)
        self._out(TUNER_START, struct.pack("<I", TUNER_REF_HZ))
        self.tune(freq_hz)
        self.set_param(R82XX_ATT, att)
        self.set_param(R82XX_VGA, vga)
        self.set_param(AD8340_VGA, 5)

    def tune(self, freq_hz):
        self._out(TUNER_TUNE, struct.pack("<Q", int(freq_hz)))

    def start(self, samplerate):
        self.samplerate = samplerate
        self.set_gpio(SHUTDOWN, False)
        self._out(ADC_SET_RATE, struct.pack("<I", int(samplerate)))
        self._out(FX3_START, b"\0" * 4)

    def stop(self):
        self._out(FX3_STOP, b"\0" * 4)
        self.set_gpio(SHUTDOWN, True)

    def read(self, n):
        """Read n real int16 ADC samples."""
        buf = bytearray()
        while len(buf) < 2 * n:
            buf += self.dev.read(0x81, 1 << 17, timeout=1000)
        return np.frombuffer(bytes(buf[: 2 * n]), dtype="<i2")

    def close(self):
        try:
            self.stop()
        finally:
            usb.util.dispose_resources(self.dev)


import usb.util  # noqa: E402  (after usb.core, used by close)
