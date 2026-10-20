#!/usr/bin/env python3
"""Report whether an RX-888 is connected, its firmware state and USB speed."""

import sys

from rx888 import usb as rxusb


def main():
    state, dev = rxusb.find()
    if state is None:
        print("No RX-888 found (looked for Cypress 04b4:00f1 and 04b4:00f3).")
        return 1
    print(f"RX-888 found: {state}, bus {dev.bus} address {dev.address}, "
          f"speed {rxusb.speed_name(dev)}")
    if state == "bootloader":
        print("Firmware not loaded yet: load fw/SDDC_FX3.img (see README).")
    if dev.speed is not None and dev.speed < 4:
        print("Warning: not on a USB 3 link; sample rate will be limited.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
