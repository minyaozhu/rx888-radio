"""Settings, read from the environment (RXAPP_*) with defaults for this Mac."""

import json
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
    # Default spectrum view: the AM broadcast band
    spectrum_center_hz: float = 1.1e6
    spectrum_bins: int = 2048
    spectrum_bin_hz: float = 1000.0

    @property
    def max_freq_hz(self):
        return RUNTIME.adc_rate_hz / 2

    @property
    def db_path(self):
        return self.data_dir / "rxapp.sqlite"

    @property
    def recordings_dir(self):
        return self.data_dir / "recordings"


# Modes offered in the UI -> radiod preset, output sample rate (None: the preset's own;
# the real rate is read back from radiod's status)
MODES = {
    "am": ("am", 12000),
    "sam": ("sam", 12000),
    "usb": ("usb", 12000),
    "lsb": ("lsb", 12000),
    "cwu": ("cwu", 12000),
    "cwl": ("cwl", 12000),
    "nfm": ("nfm", 24000),
    "wfm": ("wfm", None),
}
BAND_MODES = {"hf": ["am", "sam", "usb", "lsb", "cwu", "cwl", "nfm"], "vhf": ["wfm", "nfm", "am"]}

# radiod sample rate -> its config file. 64.8 MS/s covers 0-32 MHz, 129.6 MS/s covers 0-64 MHz.
RADIOD_CONFS = {64.8e6: "radiod-rxapp-64.conf", 129.6e6: "radiod-rxapp-129.conf"}

# VHF/UHF tuner (R828D) range
VHF_MIN_HZ, VHF_MAX_HZ = 24e6, 1700e6

CONFIG = Config()


class Runtime:
    """Settings the UI can change at run time, persisted in rxapp_data/settings.json."""

    def __init__(self, path: Path):
        self.path = path
        d = {}
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        self.adc_rate_hz = float(d.get("adc_rate_hz", 64.8e6))
        self.vga = int(d.get("vga", 6))
        self.hf_gain_db = d.get("hf_gain_db")        # None = automatic
        self.band = d.get("band", "hf")
        self.vhf_center_hz = d.get("vhf_center_hz")
        self.ft8_bands = d.get("ft8_bands")          # None = FT8 monitor off
        self.sstv_on = bool(d.get("sstv_on", False))
        self.qth = d.get("qth", "CM87")              # receiver's Maidenhead locator (FT8 map, distances)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"adc_rate_hz": self.adc_rate_hz, "vga": self.vga,
                                         "hf_gain_db": self.hf_gain_db, "band": self.band,
                                         "vhf_center_hz": self.vhf_center_hz, "ft8_bands": self.ft8_bands, "qth": self.qth, "sstv_on": self.sstv_on}))


RUNTIME = Runtime(CONFIG.data_dir / "settings.json")
