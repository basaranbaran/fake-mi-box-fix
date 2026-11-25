#!/usr/bin/env python3
"""Go/no-go test: does a candidate TOC0 carry the root key that is fused into this box?

    check_rotpk.py --fel PATH_TO_sunxi-fel CANDIDATE_TOC0 [CANDIDATE_TOC0 ...]
    check_rotpk.py --offline CANDIDATE_TOC0 [CANDIDATE_TOC0 ...]

Device mode (box in FEL mode on USB): runs `sunxi-fel readl` eight times on the ROTPK words at
0x03006270 (SID, read only) and compares them with the SHA-256 of each candidate's root public
key in every encoding below. Prints MATCH or NO MATCH per file. The device words are kept in
memory only: they are never printed and never saved. Nothing is written to the box.

Offline mode (no device): prints each candidate's checksum / signature validity and its root-key
hash in every encoding. These hashes come from public firmware files only.

Encodings tested (the H618 SBROM layout is not documented; the Lemon firmware matched the box
as n_plus_padded_e_91:direct):
    n_plus_e              SHA-256(modulus || exponent)
    n_plus_padded_e_XX    SHA-256(modulus || exponent padded to 256 bytes with byte XX)
                          for XX = 00, 91, ff
and three word orders of the device value: direct, per_word_reversed, all_reversed.

Exit status: 0 = at least one candidate matched (device mode) / all candidates parsed (offline),
1 = no match, 2 = usage or device error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import toc0

ROTPK_ADDR = 0x03006270
ROTPK_WORDS = 8


def die(message: str) -> NoReturn:
    """Print an error and exit with status 2 (usage or device error)."""
    print(f'ERROR: {message}', file=sys.stderr)
    raise SystemExit(2)


def key_encodings(report: dict) -> dict[str, bytes]:
    """Return {encoding name: SHA-256 digest} of a candidate's root public key."""
    n, e = bytes.fromhex(report['root_modulus']), bytes.fromhex(report['root_exponent'])
    blobs = {'n_plus_e': n + e}
    for pad in (0x00, 0x91, 0xFF):
        blobs['n_plus_padded_e_%02x' % pad] = n + e + bytes([pad]) * (256 - len(e))
    return {name: hashlib.sha256(blob).digest() for name, blob in blobs.items()}


def device_orders(raw: bytes) -> dict[str, bytes]:
    """The 32 device bytes in each word order that is compared."""
    return {
        'direct': raw,
        'per_word_reversed': b''.join(raw[i:i + 4][::-1] for i in range(0, 32, 4)),
        'all_reversed': raw[::-1],
    }


def read_rotpk(fel: str) -> bytes:
    """Read the 8 ROTPK words through FEL. Returns 32 bytes; never logs them."""
    cmd = [fel]
    for i in range(ROTPK_WORDS):
        cmd += ['readl', hex(ROTPK_ADDR + 4 * i)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        die(f'sunxi-fel not found: {fel}')
    except subprocess.TimeoutExpired:
        die('sunxi-fel did not answer in 30 s; is the box in FEL mode?')
    if p.returncode:
        # stderr of sunxi-fel holds USB errors only, no device values.
        die(f'sunxi-fel failed (exit {p.returncode}): {p.stderr.strip()[:300]}')
    words = [int(x, 16) for x in re.findall(r'0x[0-9a-fA-F]{8}', p.stdout)]
    if len(words) != ROTPK_WORDS:
        die(f'expected {ROTPK_WORDS} words from sunxi-fel, got {len(words)}')
    return b''.join(struct.pack('<I', w) for w in words)


def candidate(path: Path) -> dict | None:
    """inspect() a candidate; print an error line and return None if it is not a TOC0 file."""
    try:
        return toc0.inspect(path)
    except (toc0.Toc0Error, OSError) as exc:
        print(json.dumps({'file': str(path), 'error': str(exc)}))
        return None


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fel', metavar='PATH', help='sunxi-fel binary (device mode)')
    mode.add_argument('--offline', action='store_true', help='no device: show candidate facts only')
    p.add_argument('candidates', nargs='+', type=Path, metavar='CANDIDATE_TOC0')
    a = p.parse_args(argv)

    reports = [(c, candidate(c)) for c in a.candidates]
    if a.offline:
        for path, r in reports:
            if r is None:
                continue
            print(json.dumps({
                'file': str(path),
                'sha256': r['sha256'],
                'checksum_valid': r['checksum_valid'],
                'firmware_digest_present': r['firmware_digest_present'],
                'signature_valid': bool(r['certificate_signature_matches']),
                'root_key_sha256': {k: v.hex() for k, v in key_encodings(r).items()},
            }, indent=2))
        return 0 if all(r is not None for _, r in reports) else 1

    raw = read_rotpk(a.fel)
    if raw == bytes(32) or raw == b'\xff' * 32:
        print('Device ROTPK reads as all zeros or all ones: secure boot key not readable. '
              'No candidate can be judged.')
        return 2
    orders = device_orders(raw)
    del raw
    any_match = False
    for path, r in reports:
        if r is None:
            continue
        hits = [f'{enc}:{order}' for enc, digest in key_encodings(r).items()
                for order, value in orders.items() if digest == value]
        valid = toc0.is_valid(r)
        any_match |= bool(hits) and valid
        verdict = 'MATCH' if hits else 'NO MATCH'
        extra = f' ({", ".join(hits)})' if hits else ''
        if hits and not valid:
            extra += ' but the file FAILS its own checksum/signature checks: do not use it'
        print(f'{verdict:8}  {path}{extra}')
    return 0 if any_match else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
