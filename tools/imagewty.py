#!/usr/bin/env python3
"""List and unpack Allwinner IMAGEWTY firmware images (PhoenixSuit / LiveSuit .img).

    imagewty.py list    FIRMWARE.img
    imagewty.py extract FIRMWARE.img OUTDIR NAME [NAME ...]
    imagewty.py android FIRMWARE.img OUTDIR

`android` writes everything the installer needs: toc0.fex, toc1.fex, sunxi_gpt.fex and the ten
Android partition files, expands Android sparse images to raw (super.fex -> super.raw) and writes
OUTDIR/manifest.json with size and SHA-256 of each partition file. Works offline; reads only the
image. Encrypted (old LiveSuit) images are not supported.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

MAGIC = b'IMAGEWTY'
SPARSE_MAGIC = 0xED26FF3A
MiB = 1 << 20

# Firmware file -> Android partition (slot A) it belongs to.
ANDROID_PARTS = {
    'boot-resource.fex': 'bootloader_a',
    'env.fex': 'env_a',
    'boot.fex': 'boot_a',
    'vendor_boot.fex': 'vendor_boot_a',
    'super.fex': 'super',
    'misc.fex': 'misc',
    'vbmeta.fex': 'vbmeta_a',
    'vbmeta_system.fex': 'vbmeta_system_a',
    'vbmeta_vendor.fex': 'vbmeta_vendor_a',
    'dtbo.fex': 'dtbo_a',
}
BOOT_FILES = ['toc0.fex', 'toc1.fex', 'sunxi_gpt.fex']


@dataclass
class Item:
    maintype: str
    subtype: str
    name: str
    offset: int
    length: int


def read_items(image: Path) -> list[Item]:
    with image.open('rb') as f:
        head = f.read(1024)
        if head[:8] != MAGIC:
            raise SystemExit(f'{image}: not an unencrypted IMAGEWTY image')
        version, = struct.unpack_from('<I', head, 8)
        if version != 0x300:
            raise SystemExit(f'{image}: header version {version:#x} not supported (expected 0x300)')
        count = struct.unpack_from('<13I', head, 32)[7]
        items = []
        for _ in range(count):
            fh = f.read(1024)
            name = fh[36:292].split(b'\0')[0].decode()
            _stored, _pad1, original, _pad2, offset = struct.unpack_from('<5I', fh, 292)
            items.append(Item(fh[8:16].rstrip(b'\0 ').decode(errors='replace'),
                              fh[16:32].rstrip(b'\0 ').decode(errors='replace'), name, offset, original))
    return items


def copy_out(image: Path, item: Item, dest: Path) -> None:
    with image.open('rb') as src, dest.open('wb') as out:
        src.seek(item.offset)
        left = item.length
        while left:
            block = src.read(min(left, 4 * MiB))
            if not block:
                raise SystemExit(f'{item.name}: image is truncated')
            out.write(block)
            left -= len(block)


def unsparse(path: Path) -> Path:
    """Expand an Android sparse image next to itself (.raw). Returns the raw path, or path if not sparse."""
    with path.open('rb') as f:
        head = f.read(28)
        if len(head) < 28 or struct.unpack_from('<I', head)[0] != SPARSE_MAGIC:
            return path
        _magic, major, _minor, fhdr, chdr, bsize, total, chunks, _crc = struct.unpack('<I4H4I', head)
        if major != 1 or fhdr < 28 or chdr < 12 or bsize > MiB:
            raise SystemExit(f'{path.name}: unexpected sparse header')
        f.seek(fhdr)
        raw = path.with_suffix('.raw')
        blocks = 0
        with raw.open('wb') as out:
            for _ in range(chunks):
                ctype, _res, nblk, size = struct.unpack_from('<2H2I', f.read(chdr))
                length = nblk * bsize
                if ctype == 0xCAC1:  # raw data
                    if size != chdr + length:
                        raise SystemExit('bad raw chunk')
                    left = length
                    while left:
                        b = f.read(min(left, MiB))
                        if not b:
                            raise SystemExit('sparse image truncated')
                        out.write(b)
                        left -= len(b)
                elif ctype == 0xCAC2:  # fill
                    pattern = f.read(4) * (MiB // 4)
                    left = length
                    while left:
                        n = min(left, len(pattern))
                        out.write(pattern[:n])
                        left -= n
                elif ctype == 0xCAC3:  # don't care
                    out.seek(length, 1)
                elif ctype == 0xCAC4:  # crc32
                    f.read(4)
                else:
                    raise SystemExit(f'unknown sparse chunk {ctype:#x}')
                blocks += nblk
            if blocks != total:
                raise SystemExit('sparse block count mismatch')
            out.truncate(total * bsize)
    return raw


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while b := f.read(4 * MiB):
            h.update(b)
    return h.hexdigest()


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] not in ('list', 'extract', 'android'):
        raise SystemExit(__doc__)
    cmd, image = argv[0], Path(argv[1])
    items = {i.name: i for i in read_items(image)}
    if cmd == 'list':
        for i in items.values():
            print(f'{i.name:28} {i.length:>12}  {i.maintype}/{i.subtype}')
        return
    outdir = Path(argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    names = argv[3:] if cmd == 'extract' else BOOT_FILES + list(ANDROID_PARTS)
    missing = [n for n in names if n not in items]
    if missing:
        raise SystemExit(f'not in image: {", ".join(missing)}')
    manifest = []
    for name in names:
        dest = outdir / name
        copy_out(image, items[name], dest)
        print(f'extracted {name} ({items[name].length} bytes)', flush=True)
        if cmd == 'android' and name in ANDROID_PARTS:
            final = unsparse(dest)
            if final != dest:
                dest.unlink()  # keep only the raw image
                print(f'  expanded sparse image -> {final.name}', flush=True)
            manifest.append({'partition': ANDROID_PARTS[name], 'file': final.name,
                             'bytes': final.stat().st_size, 'sha256': sha256(final)})
    if cmd == 'android':
        (outdir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'wrote {outdir / "manifest.json"}')


if __name__ == '__main__':
    main(sys.argv[1:])
