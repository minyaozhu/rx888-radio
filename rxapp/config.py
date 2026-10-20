"""Settings, read from the environment (RXAPP_*) with defaults for this Mac."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name, default):
    return os.environ.get(f"RXAPP_{name}", default)


@dataclass(frozen=True)
class Config:
    # radiod's status/command group (its "status =" name in radiod-rx888.conf)
    status_name: str = _env("STATUS", "rx888-status.local")
    # radiod runs with ttl = 0, so all multicast stays on the loopback interface
    iface_addr: str = _env("IFACE", "127.0.0.1")
    # Group our channels send audio to; derived from this name like radiod does
    pcm_name: str = _env("PCM", "rxapp-pcm.local")
    rtp_port: int = 5004
    # Web server
    host: str = _env("HOST", "127.0.0.1")
    port: int = int(_env("PORT", "8090"))
    # radiod process management
    radiod_script: Path = ROOT / "ka9q" / "run_radiod.sh"
    manage_radiod: bool = _env("MANAGE_RADIOD", "1") == "1"
    # Data
    data_dir: Path = Path(_env("DATA", str(ROOT / "rxapp_data")))
    # radiod's sample rate decides the HF coverage (Nyquist)
    adc_rate_hz: float = float(_env("ADC_RATE", "64.8e6"))
    # Default spectrum view: the AM broadcast band
    spectrum_center_hz: float = 1.1e6
    spectrum_bins: int = 2048
    spectrum_bin_hz: float = 1000.0

    @property
    def max_freq_hz(self):
        return self.adc_rate_hz / 2

    @property
    def db_path(self):
        return self.data_dir / "rxapp.sqlite"

    @property
    def recordings_dir(self):
        return self.data_dir / "recordings"


# Modes offered in the UI -> radiod preset, output sample rate
MODES = {
    "am": ("am", 12000),
    "sam": ("sam", 12000),
    "usb": ("usb", 12000),
    "lsb": ("lsb", 12000),
    "cwu": ("cwu", 12000),
    "cwl": ("cwl", 12000),
    "nfm": ("nfm", 24000),
}

CONFIG = Config()
