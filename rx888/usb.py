"""Find the RX-888 on USB.

The RX-888 is built on a Cypress FX3. Fresh from power-up it enumerates as
the FX3 bootloader and needs its firmware (SDDC_FX3.img) loaded into RAM;
after that it re-enumerates as the SDDC device. This happens on every plug-in.
"""

import usb.core

CYPRESS_VID = 0x04B4
FX3_BOOTLOADER_PID = 0x00F3  # no firmware loaded yet
SDDC_PID = 0x00F1            # running SDDC firmware


def find():
    """Return (state, device): state is 'sddc', 'bootloader', or None."""
    dev = usb.core.find(idVendor=CYPRESS_VID, idProduct=SDDC_PID)
    if dev is not None:
        return "sddc", dev
    dev = usb.core.find(idVendor=CYPRESS_VID, idProduct=FX3_BOOTLOADER_PID)
    if dev is not None:
        return "bootloader", dev
    return None, None


def speed_name(dev):
    """USB link speed; the RX-888 needs SuperSpeed (USB 3) for full rate."""
    names = {1: "low", 2: "full", 3: "high (USB 2)", 4: "super (USB 3)", 5: "super+"}
    return names.get(getattr(dev, "speed", None), "unknown")
