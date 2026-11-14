#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract the SPLC static resource (band1) from a Sharp Windows driver.

Why this script exists
----------------------
The SPLC job stream starts with a 7104-byte static resource block. It is vendor
data taken from Sharp's own driver, so it is **not** redistributed in this
repository. Extract it yourself from a driver you obtained legally.

SPLC frame layout (as used by the print stream):

    81 81 00 00 | len (2 bytes, big endian) | subtype (2 bytes, big endian) | payload

`sd3bnp2.dat` inside the Windows SPLC driver contains the same frames:
subtype 0x000a is the setup string, subtype 0x0001 is the resource block we need.

Usage
-----
    python3 tools/extract-band1.py sd3bnp2.dat -o band1.bin
    python3 tools/extract-band1.py sd3bnp2.dat -o band1.bin --list

Expected result: 7104 bytes, md5 b7404defe761cfd368aa7f2821ba4557 (AR-2221R family).

If your driver layout differs, run with --list first and pick the frame by hand.
You can also copy band1.bin from the upstream Swift project
(https://github.com/zy2n9nzt5b-hue/SharpSPLCBridge), which ships the same bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

MAGIC = b"\x81\x81\x00\x00"
KNOWN_MD5 = "b7404defe761cfd368aa7f2821ba4557"
KNOWN_SIZE = 7104


def iter_frames(data: bytes):
    """Yield (offset, subtype, payload) for every well-formed frame in data."""
    start = 0
    while True:
        i = data.find(MAGIC, start)
        if i < 0:
            return
        start = i + 4
        if i + 8 > len(data):
            return
        length, subtype = struct.unpack_from(">HH", data, i + 4)
        payload = data[i + 8:i + 8 + length]
        if length == 0 or len(payload) != length:
            continue
        if subtype > 0x00FF:
            continue
        yield i, subtype, payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract the SPLC band1 resource from a Sharp driver file")
    ap.add_argument("input", help="driver file, e.g. sd3bnp2.dat")
    ap.add_argument("-o", "--out", default="band1.bin")
    ap.add_argument("--list", action="store_true", help="only list frames, do not write")
    a = ap.parse_args()

    src = Path(a.input).expanduser()
    if not src.is_file():
        sys.exit("not a file: %s" % src)
    data = src.read_bytes()

    frames = list(iter_frames(data))
    if not frames:
        sys.exit("no SPLC frames found in %s (tried magic %s)" % (src.name, MAGIC.hex(" ")))

    if a.list:
        print("%-10s %-8s %-8s %s" % ("offset", "subtype", "size", "md5"))
        for off, sub, payload in frames:
            print("%-10d 0x%04x  %-8d %s" % (off, sub, len(payload), hashlib.md5(payload).hexdigest()))
        return 0

    candidates = [f for f in frames if f[1] == 0x0001]
    if not candidates:
        sys.exit("no subtype 0x0001 frame; run with --list to inspect")

    pick = None
    for off, sub, payload in candidates:
        if hashlib.md5(payload).hexdigest() == KNOWN_MD5:
            pick = (off, payload)
            break
    if pick is None:
        for off, sub, payload in candidates:
            if len(payload) == KNOWN_SIZE:
                pick = (off, payload)
                break
    if pick is None:
        pick = max(((off, p) for off, _, p in candidates), key=lambda x: len(x[1]))

    off, payload = pick
    out = Path(a.out).expanduser()
    out.write_bytes(payload)
    digest = hashlib.md5(payload).hexdigest()
    print("wrote %s: %d bytes, md5 %s" % (out, len(payload), digest))
    if digest != KNOWN_MD5:
        print("warning: md5 differs from the known-good AR-2221R resource (%s)" % KNOWN_MD5)
        print("         the size may still be right; check --list for other 0x0001 frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
