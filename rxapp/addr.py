# SPDX-License-Identifier: GPL-3.0-or-later
"""radiod's name -> multicast address mapping.

radiod picks every group address by hashing its name (make_maddr in multicast.c), so a
client can compute the address instead of asking mDNS, which doesn't resolve radiod's
groups on macOS.
"""


def _fnv1(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= b
    return h


def maddr(name: str) -> str:
    """Multicast address radiod uses for `name` (e.g. 'am.local')."""
    a = (239 << 24) | (_fnv1(name.encode()) & 0xFFFFFF)
    # radiod avoids 239.0.0.0/24 and 239.128.0.0/24 (they alias 224.0.0.0/24 at layer 2)
    if (a & 0x007FFF00) == 0:
        a |= (a & 0xFF) << 8
    if (a & 0x007FFF00) == 0:
        a |= 0x00100000
    return ".".join(str((a >> s) & 0xFF) for s in (24, 16, 8, 0))
