#!/usr/bin/env python3
"""Put the S3_V2.0 DRAM settings into a Lemon TV Mini H618 TOC0 (SPL) file. Offline, no device.

    patch_toc0_dram.py SRC_TOC0 DST_TOC0 [--dram-clk 600] [--dx-odt 0x06010609]
                       [--dx-dri 0x0d090d0d] [--tpr10 0x00ef3359]
                       [--expect-source-sha256 HEX | --any-source]

The DRAM parameter block of a TOC0 file sits in the TOC0 header, outside the signed certificate
and firmware items. This tool changes only four 32-bit words (DRAM clock, DX_ODT, DX_DRI, TPR10)
and the header checksum. It then checks that no other byte changed and that the checksum, the
firmware hash and the RSA signature are still valid (toc0.inspect).

The defaults are the values measured on the S3_V2.0 board. With the Lemon v12.1.1.604 TOC0
(SHA-256 70d3b039...) the output SHA-256 is 64283fc194ecb909b7168dadb6ff797741016ceee4a2074e49ccd2e84629b94c.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
from pathlib import Path

import toc0

LEMON_TOC0_SHA256 = '70d3b039c0e82dc06cd310cfb4abf93d5263f2df5fa47fb5b3084496e8f3032c'
S3_TOC0_SHA256 = '64283fc194ecb909b7168dadb6ff797741016ceee4a2074e49ccd2e84629b94c'

# name -> (offset in the TOC0 file, value in the Lemon v12.1.1.604 file)
FIELDS: dict[str, tuple[int, int]] = {
    'dram_clk': (0x84, 720),
    'dx_odt': (0x8C, 0x06060606),
    'dx_dri': (0x90, 0x0D0D0D0D),
    'tpr10': (0xF0, 0x002F3359),
}
DRAM_TYPE_OFFSET = 0x88  # read only; 7 = LPDDR3
CHECKSUM_OFFSET = 12


def read_dram_fields(data: bytes) -> dict[str, int]:
    """Return the four DRAM words and the DRAM type of a TOC0 image."""
    out = {name: struct.unpack_from('<I', data, at)[0] for name, (at, _) in FIELDS.items()}
    out['dram_type'] = struct.unpack_from('<I', data, DRAM_TYPE_OFFSET)[0]
    return out


def patch(data: bytes, values: dict[str, int], strict: bool) -> bytes:
    """Return a copy of `data` with the DRAM words in `values` and a new checksum.

    With strict=True every field must still hold the Lemon v12.1.1.604 value before the change.
    """
    d = bytearray(data)
    for name, new in values.items():
        at, lemon = FIELDS[name]
        old = struct.unpack_from('<I', d, at)[0]
        if strict and old != lemon:
            raise SystemExit(f'{name} at {at:#x} is {old:#x}, expected {lemon:#x}; wrong source file?')
        struct.pack_into('<I', d, at, new)
    size = struct.unpack_from('<I', d, 28)[0]
    struct.pack_into('<I', d, CHECKSUM_OFFSET, toc0.checksum(bytes(d[:size])))
    allowed = {CHECKSUM_OFFSET} | {at for at, _ in FIELDS.values()}
    changed = {i // 4 * 4 for i in range(len(d)) if d[i] != data[i]}
    if not changed <= allowed:
        raise SystemExit(f'internal error: unexpected bytes changed at {sorted(changed - allowed)}')
    return bytes(d)


def parse_args(argv: list[str]) -> argparse.Namespace:
    num = lambda s: int(s, 0)  # noqa: E731 - accepts 600, 0x258, ...
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('src', type=Path, help='source TOC0 (toc0.fex from the Lemon image)')
    p.add_argument('dst', type=Path, help='output TOC0 (for install.py: WORKDIR/toc0-s3.fex)')
    p.add_argument('--dram-clk', type=num, default=600, help='DRAM clock in MHz (default 600)')
    p.add_argument('--dx-odt', type=num, default=0x06010609, help='DX_ODT (default 0x06010609)')
    p.add_argument('--dx-dri', type=num, default=0x0D090D0D, help='DX_DRI (default 0x0d090d0d)')
    p.add_argument('--tpr10', type=num, default=0x00EF3359, help='TPR10 (default 0x00ef3359)')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--expect-source-sha256', default=LEMON_TOC0_SHA256, metavar='HEX',
                   help='refuse a source with another SHA-256 (default: Lemon v12.1.1.604 TOC0)')
    g.add_argument('--any-source', action='store_true',
                   help='skip the source SHA-256 and old-value checks (not tested)')
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    a = parse_args(argv)
    if a.src.resolve() == a.dst.resolve():
        raise SystemExit('DST must be a different file than SRC')
    src = a.src.read_bytes()
    src_sha = hashlib.sha256(src).hexdigest()
    before = toc0.inspect(a.src)
    if not toc0.is_valid(before):
        raise SystemExit(f'{a.src}: source TOC0 fails the offline checks; refusing')
    if not a.any_source and src_sha != a.expect_source_sha256.lower():
        raise SystemExit(f'{a.src}: SHA-256 {src_sha} is not the expected {a.expect_source_sha256}.\n'
                         'Use the TOC0 from the Lemon TV Mini H618 v12.1.1.604 image, or pass --any-source.')
    values = {'dram_clk': a.dram_clk, 'dx_odt': a.dx_odt, 'dx_dri': a.dx_dri, 'tpr10': a.tpr10}
    for name, v in values.items():
        if not 0 <= v <= 0xFFFFFFFF:
            raise SystemExit(f'--{name.replace("_", "-")} out of range')
    old = read_dram_fields(src)
    out = patch(src, values, strict=not a.any_source)
    tmp = a.dst.with_name(a.dst.name + '.part')
    tmp.write_bytes(out)
    after = toc0.inspect(tmp)
    if not (toc0.is_valid(after) and after['certificate_signature_matches'] == before['certificate_signature_matches']):
        tmp.unlink()
        raise SystemExit('patched file fails the offline checks; nothing written')
    os.replace(tmp, a.dst)
    print(f'source  {a.src}  sha256 {src_sha}')
    print(f'dram_type {old["dram_type"]} (7 = LPDDR3), unchanged')
    for name, v in values.items():
        print(f'{name:9} {old[name]:#010x} -> {v:#010x}' if name != 'dram_clk'
              else f'{name:9} {old[name]} MHz -> {v} MHz')
    print(f'output  {a.dst}  sha256 {after["sha256"]}')
    print('checksum valid, firmware hash present, signature valid '
          f'({",".join(after["certificate_signature_matches"])}); only 4 DRAM words + checksum changed')
    if after['sha256'] == S3_TOC0_SHA256:
        print('output equals the TOC0 tested on the S3_V2.0 box')
    else:
        print('NOTE: output differs from the TOC0 tested on the S3_V2.0 box (64283fc1...)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
