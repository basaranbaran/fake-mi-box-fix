#!/usr/bin/env python3
"""Offline checks for Allwinner secure boot files (TOC0 = SPL, TOC1 = U-Boot package). No device access.

    toc0.py FILE [FILE ...]   -> JSON per file

For a TOC0 file: checksum, firmware hash present in the certificate, RSA signature of the
certificate, optional key item. For a TOC1 file ("sunxi-secure" package): magic, end marker and
checksum. Root key values are never printed here; check_rotpk.py uses them.

Also imported by check_rotpk.py, patch_toc0_dram.py and install.py.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any

CHECKSUM_SEED = 0x5F0A6C39
ITEM_CERT, ITEM_FIRMWARE, ITEM_KEY = 0x10101, 0x10202, 0x10303
TOC1_MAGIC = 0x89119800
TOC1_END = 0x3B45494D  # "MIE;"


class Toc0Error(ValueError):
    """The file is not a TOC0/TOC1 file or its structure is not understood."""


def _tlv(buf: bytes, at: int) -> tuple[int, int, int]:
    """Read one DER tag/length at `at`. Returns (tag, value offset, value length)."""
    tag, n = buf[at], buf[at + 1]
    head = 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[at + 2:at + 2 + k], 'big')
        head += k
    return tag, at + head, n


def checksum(data: bytes, field_offset: int = 12) -> int:
    """Allwinner boot checksum: 32-bit sum of all words with the checksum field set to the seed."""
    words = list(struct.unpack('<%dI' % (len(data) // 4), data[:len(data) // 4 * 4]))
    words[field_offset // 4] = CHECKSUM_SEED
    return sum(words) & 0xFFFFFFFF


def inspect(path: str | Path) -> dict[str, Any]:
    """Parse a TOC0 file and run all offline checks.

    Returns a dict with: file, bytes, items, sha256, checksum_valid, firmware_digest_present,
    certificate_signature_matches (list of matching signed ranges, empty = invalid),
    key_item_signature_valid (None if there is no key item), root_modulus / root_exponent (hex).
    Raises Toc0Error if the file is not a TOC0 file.
    """
    d = Path(path).read_bytes()
    if d[:8] != b'TOC0.GLH':
        raise Toc0Error(f'{path}: not a TOC0 file')
    count, size = struct.unpack_from('<II', d, 24)
    if not (1 <= count <= 8 and 48 + count * 32 <= size <= len(d)):
        raise Toc0Error(f'{path}: bad TOC0 header')
    d = d[:size]
    items = {}
    for i in range(count):
        ident, offset, length = struct.unpack_from('<III', d, 48 + 32 * i)
        if offset + length > size:
            raise Toc0Error(f'{path}: item {ident:#x} runs past the end of the file')
        items[ident] = d[offset:offset + length]
    if ITEM_CERT not in items or ITEM_FIRMWARE not in items:
        raise Toc0Error(f'{path}: certificate or firmware item missing')
    cert, firmware = items[ITEM_CERT], items[ITEM_FIRMWARE]
    pos = cert.find(b'\x02\x82\x01\x00')  # RSA-2048 modulus
    if pos < 0:
        raise Toc0Error(f'{path}: no RSA-2048 key in the certificate')
    n_bytes = cert[pos + 4:pos + 260]
    _, ev, el = _tlv(cert, pos + 260)
    e_bytes = cert[ev:ev + el]
    n, e = int.from_bytes(n_bytes, 'big'), int.from_bytes(e_bytes, 'big')
    _, outer, _ = _tlv(cert, 0)
    _, tbs, tbslen = _tlv(cert, outer)
    _, ov, olen = _tlv(cert, 0)
    signature = cert[ov + olen - 256:ov + olen]
    recovered = pow(int.from_bytes(signature, 'big'), e, n).to_bytes(256, 'big')[-32:]
    # Allwinner certificates use non-standard framing; report which signed range matches.
    ranges = {'sbrom_tbs': cert[outer:outer + tbslen], 'full_tbs': cert[outer:tbs + tbslen]}
    sig_ok = [k for k, v in ranges.items() if hashlib.sha256(v).digest() == recovered]
    root_n, root_e, key_ok = n_bytes, e_bytes, None
    if ITEM_KEY in items:  # optional key item: root key signs the certificate key
        key = items[ITEM_KEY]
        _, n0, e0, n1, _e1, sl = struct.unpack_from('<6I', key)
        if not (n0 == n1 == 256 and sl == 256):
            raise Toc0Error(f'{path}: unexpected key item layout')
        root_n, root_e = key[24:24 + n0], key[24 + n0:24 + n0 + e0]
        dec = pow(int.from_bytes(key[-256:], 'big'), int.from_bytes(root_e, 'big'),
                  int.from_bytes(root_n, 'big')).to_bytes(256, 'big')[-32:]
        key_ok = dec == hashlib.sha256(key[:-256]).digest()
    return {
        'file': str(path),
        'bytes': size,
        'items': count,
        'sha256': hashlib.sha256(d).hexdigest(),
        'checksum_valid': struct.unpack_from('<I', d, 12)[0] == checksum(d),
        'firmware_digest_present': hashlib.sha256(firmware).digest() in cert,
        'certificate_signature_matches': sig_ok,
        'key_item_signature_valid': key_ok,
        'root_modulus': root_n.hex(),
        'root_exponent': root_e.hex(),
    }


def is_valid(report: dict[str, Any]) -> bool:
    """True when every offline TOC0 check in an inspect() report passed."""
    return bool(report['checksum_valid'] and report['firmware_digest_present']
                and report['certificate_signature_matches']
                and report['key_item_signature_valid'] in (None, True))


def inspect_toc1(path: str | Path) -> dict[str, Any]:
    """Check a TOC1 ("sunxi-secure") package: magic, end marker, length and checksum."""
    d = Path(path).read_bytes()
    if len(d) < 64 or not d.startswith(b'sunxi-secure'):
        raise Toc0Error(f'{path}: not a TOC1 file')
    magic, add_sum, _serial, _status, items, valid_len = struct.unpack_from('<6I', d, 16)
    end, = struct.unpack_from('<I', d, 60)
    ok_len = 64 <= valid_len <= len(d) and valid_len % 4 == 0
    return {
        'file': str(path),
        'bytes': len(d),
        'items': items,
        'valid_len': valid_len,
        'sha256': hashlib.sha256(d).hexdigest(),
        'magic_valid': magic == TOC1_MAGIC and end == TOC1_END,
        'checksum_valid': ok_len and checksum(d[:valid_len], 20) == add_sum,
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    status = 0
    for p in argv:
        head = Path(p).read_bytes()[:12]
        try:
            if head.startswith(b'sunxi-secure'):
                r = inspect_toc1(p)
                ok = r['magic_valid'] and r['checksum_valid']
            else:
                r = inspect(p)
                ok = is_valid(r)
                r = {k: v for k, v in r.items() if not k.startswith('root_')}
        except Toc0Error as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            status = 1
            continue
        r['all_checks_passed'] = ok
        print(json.dumps(r, indent=2))
        status |= 0 if ok else 1
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
