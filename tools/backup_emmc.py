#!/usr/bin/env python3
"""Full eMMC backup of the box over the RAM Linux USB network. Reads only; never writes the box.

    backup_emmc.py BACKUPDIR [--box 192.168.77.1]

Needs the RAM Linux running (boot_ramlinux.py) and the USB network up (the host gets
192.168.77.10 by DHCP). The box side is ramlinux/rootfs/s3backup.cgi:
  GET /cgi-bin/s3backup?info    disk name, sector count, chunk count, boot partition sizes
  GET /cgi-bin/s3backup?boot0   eMMC boot partition 0 (boot1 the same)
  GET /cgi-bin/s3backup?N       user area chunk N (256 MiB, the last one may be shorter)
Each data answer carries X-SHA256. A file is kept only when its length and SHA-256 match.

BACKUPDIR gets (folder mode 700, files mode 600):
  info.json       the ?info answer
  boot0.bin, boot1.bin, chunk-00.bin ... chunk-NN.bin
  manifest.json   [{file, source, offset, bytes, sha256}, ...] for every verified file

Run it again to resume: files already in manifest.json whose size and SHA-256 still match are
skipped. A backup of another eMMC (different name or size) in the same folder is refused.
Restore: `cat chunk-*.bin` is the whole user area in order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MiB = 1 << 20
ATTEMPTS = 3


def fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while b := f.read(4 * MiB):
            h.update(b)
    return h.hexdigest()


def write_private(path: Path, text: str) -> None:
    """Write a small text file with mode 600, atomically."""
    tmp = path.with_name(path.name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def plan(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Every file of a complete backup: file, source (query), offset in the source, bytes."""
    disk = int(info['sectors']) * 512
    chunk = int(info['chunk_mib']) * MiB
    items = [{'file': f'{b}.bin', 'source': b, 'offset': 0, 'bytes': int(info[f'{b}_bytes'])}
             for b in ('boot0', 'boot1')]
    for n in range(int(info['chunks'])):
        items.append({'file': f'chunk-{n:02d}.bin', 'source': str(n), 'offset': n * chunk,
                      'bytes': min(chunk, disk - n * chunk)})
    if sum(i['bytes'] for i in items[2:]) != disk:
        raise SystemExit('info.json is inconsistent: chunks do not cover the disk')
    return items


def download(url: str, dest: Path, size: int) -> str:
    """Download one item to dest; verify length and X-SHA256. Returns the SHA-256."""
    partial = dest.with_name(dest.name + '.part')
    with urllib.request.urlopen(url, timeout=300) as r:
        expected = (r.headers.get('X-SHA256') or '').strip().lower()
        length = int(r.headers.get('Content-Length', -1))
        if len(expected) != 64 or length != size:
            raise ValueError(f'bad answer headers (X-SHA256 {expected!r}, length {length}, want {size})')
        h, n = hashlib.sha256(), 0
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            while b := r.read(MiB):
                f.write(b)
                h.update(b)
                n += len(b)
            f.flush()
            os.fsync(f.fileno())
    if n != size or h.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise ValueError(f'received {n} bytes, SHA-256 {"ok" if h.hexdigest() == expected else "MISMATCH"}')
    os.replace(partial, dest)
    return expected


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('backupdir', type=Path, metavar='BACKUPDIR')
    p.add_argument('--box', default='192.168.77.1', help='box address on the USB network (default 192.168.77.1)')
    a = p.parse_args(argv)
    base = f'http://{a.box}/cgi-bin/s3backup?'
    out: Path = a.backupdir

    try:
        info = fetch_json(base + 'info')
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SystemExit(f'cannot read {base}info: {exc}\nIs the RAM Linux running and the USB network up?') from None
    for key in ('name', 'sectors', 'chunk_mib', 'chunks', 'boot0_bytes', 'boot1_bytes'):
        if key not in info:
            raise SystemExit(f'?info answer lacks {key!r}')
    items = plan(info)

    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    info_path, manifest_path = out / 'info.json', out / 'manifest.json'
    if info_path.exists():
        old = json.loads(info_path.read_text())
        same = all(old.get(k) == info[k] for k in ('name', 'sectors', 'chunk_mib', 'boot0_bytes', 'boot1_bytes'))
        if not same:
            raise SystemExit(f'{out} holds a backup of another eMMC ({old.get("name")}, {old.get("sectors")} sectors); '
                             'use an empty folder')
    write_private(info_path, json.dumps(info, indent=2) + '\n')
    manifest: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        manifest = {m['file']: m for m in json.loads(manifest_path.read_text())}

    total = sum(i['bytes'] for i in items)
    have = sum(m['bytes'] for m in manifest.values())
    free = shutil.disk_usage(out).free
    if free < total - have + 64 * MiB:
        raise SystemExit(f'not enough free space in {out}: need {(total - have) // MiB} MiB, have {free // MiB} MiB')

    print(f'eMMC {info["name"]}: {info["sectors"]} sectors ({total // MiB} MiB with boot0/boot1), '
          f'{len(items)} files', flush=True)
    started = time.monotonic()
    for item in items:
        dest = out / item['file']
        known = manifest.get(item['file'])
        if known and known['bytes'] == item['bytes'] and dest.is_file() \
                and dest.stat().st_size == item['bytes'] and sha256_file(dest) == known['sha256']:
            print(f'skip     {item["file"]} (already verified)', flush=True)
            continue
        for attempt in range(1, ATTEMPTS + 1):
            try:
                digest = download(base + item['source'], dest, item['bytes'])
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print(f'retry    {item["file"]} attempt {attempt}/{ATTEMPTS}: {exc}', flush=True)
                if attempt == ATTEMPTS:
                    raise SystemExit(f'giving up on {item["file"]}; run the same command again to resume') from None
                time.sleep(3)
        os.chmod(dest, 0o600)
        manifest[item['file']] = {**item, 'sha256': digest}
        write_private(manifest_path, json.dumps([manifest[i['file']] for i in items if i['file'] in manifest],
                                                indent=2) + '\n')
        print(f'verified {item["file"]} {item["bytes"]} bytes sha256 {digest[:16]}...', flush=True)
    missing = [i['file'] for i in items if i['file'] not in manifest]
    if missing:
        raise SystemExit(f'incomplete: {missing}')
    print(f'BACKUP_COMPLETE {len(items)} files verified in {out} ({time.monotonic() - started:.0f} s)')
    print('Keep this folder. It holds data unique to your box.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
