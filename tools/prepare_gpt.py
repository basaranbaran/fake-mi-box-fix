#!/usr/bin/env python3
"""Fit the Lemon donor GPT to this box's eMMC. Offline; reads files only.

    prepare_gpt.py --donor WORKDIR/sunxi_gpt.fex --backup BACKUPDIR --out WORKDIR
    prepare_gpt.py --donor WORKDIR/sunxi_gpt.fex --old-gpt OLD_DISK_HEAD.bin --out WORKDIR

The donor GPT (sunxi_gpt.fex from the Lemon image) is made for an 8 GB card and names the data
partition UDISK. This tool:
  * renames UDISK to userdata (the Lemon vendor_boot fstab mounts /dev/block/by-name/userdata),
  * extends userdata to the last usable LBA of this disk (disk sectors - 34),
  * keeps the disk GUID of the box's old GPT,
  * gives every partition a deterministic GUID: uuid5(disk GUID, partition name),
  * writes WORKDIR/gpt-primary.bin (LBA 0-15: protective MBR, header, entries),
    WORKDIR/gpt-backup.bin (entries + backup header, written at the end of the disk) and
    WORKDIR/layout.json (disk size, backup GPT LBA, partition table).

The disk size and old GPT come from a backup_emmc.py backup (info.json + chunk-00.bin). For
tests, --old-gpt takes a raw dump of at least the first 34 sectors; then the disk size is read
from the old GPT header (backup header LBA + 1).

The module is also imported by install.py (parse_gpt, Gpt, Partition).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

SECTOR = 512
PRIMARY_SECTORS = 16  # gpt-primary.bin covers LBA 0-15; the boot TOC0 copy starts at LBA 16
RESERVED_TAIL = 33    # last usable LBA = disk sectors - 1 - 33 (room for a 128-entry backup GPT)


@dataclass(frozen=True)
class Partition:
    """One used GPT entry."""
    index: int
    name: str
    start_lba: int
    end_lba: int
    type_guid: bytes
    part_guid: bytes

    @property
    def bytes(self) -> int:
        return (self.end_lba - self.start_lba + 1) * SECTOR


@dataclass(frozen=True)
class Gpt:
    """A parsed and CRC-checked GPT header with its entry array."""
    header: bytes           # header_size bytes
    my_lba: int
    alternate_lba: int
    first_usable: int
    last_usable: int
    disk_guid: uuid.UUID
    entries_lba: int
    entry_count: int
    entry_size: int
    entries: bytes
    partitions: tuple[Partition, ...]

    @property
    def entry_sectors(self) -> int:
        return -(-self.entry_count * self.entry_size // SECTOR)

    @property
    def primary_region_sectors(self) -> int:
        """Sectors from LBA 0 to the end of the primary entry array (MBR + header + entries)."""
        return self.entries_lba + self.entry_sectors

    def by_name(self) -> dict[str, Partition]:
        return {p.name: p for p in self.partitions}


def parse_header(hdr: bytes, where: str) -> tuple[int, ...]:
    """Check a GPT header block; return (size, my, alt, first, last, entries_lba, count, esize, ecrc)."""
    if hdr[:8] != b'EFI PART':
        raise ValueError(f'{where}: no GPT header signature')
    size, crc = struct.unpack_from('<II', hdr, 12)
    if not 92 <= size <= SECTOR:
        raise ValueError(f'{where}: bad GPT header size {size}')
    check = bytearray(hdr[:size])
    struct.pack_into('<I', check, 16, 0)
    if zlib.crc32(check) & 0xFFFFFFFF != crc:
        raise ValueError(f'{where}: GPT header CRC mismatch')
    my, alt, first, last = struct.unpack_from('<4Q', hdr, 24)
    elba, count, esize, ecrc = struct.unpack_from('<QIII', hdr, 72)
    if esize != 128 or not 1 <= count <= 128:
        raise ValueError(f'{where}: unsupported GPT entry layout ({count} x {esize})')
    return size, my, alt, first, last, elba, count, esize, ecrc


def parse_entries(entries: bytes, count: int, esize: int, ecrc: int, where: str) -> tuple[Partition, ...]:
    """Check the entry array CRC and return the used entries in table order."""
    if len(entries) < count * esize or zlib.crc32(entries[:count * esize]) & 0xFFFFFFFF != ecrc:
        raise ValueError(f'{where}: GPT entry array CRC mismatch')
    parts = []
    for i in range(count):
        e = entries[i * esize:(i + 1) * esize]
        if not any(e[:16]):
            continue
        start, end = struct.unpack_from('<QQ', e, 32)
        name = e[56:128].decode('utf-16le').split('\0')[0]
        parts.append(Partition(i, name, start, end, bytes(e[:16]), bytes(e[16:32])))
    return tuple(parts)


def parse_gpt(disk_head: bytes, where: str = 'GPT') -> Gpt:
    """Parse the primary GPT from the first sectors of a disk (or a sunxi_gpt.fex file)."""
    if len(disk_head) < 2 * SECTOR or disk_head[510:512] != b'\x55\xaa':
        raise ValueError(f'{where}: no protective MBR')
    hdr = disk_head[SECTOR:2 * SECTOR]
    size, my, alt, first, last, elba, count, esize, ecrc = parse_header(hdr, where)
    if my != 1:
        raise ValueError(f'{where}: primary header is not at LBA 1')
    entries = disk_head[elba * SECTOR:elba * SECTOR + count * esize]
    parts = parse_entries(entries, count, esize, ecrc, where)
    return Gpt(bytes(hdr[:size]), my, alt, first, last, uuid.UUID(bytes_le=bytes(hdr[56:72])),
               elba, count, esize, bytes(entries), parts)


def partition_guid(disk_guid: uuid.UUID, name: str) -> uuid.UUID:
    """Deterministic partition GUID: the same disk and name always give the same GUID."""
    return uuid.uuid5(disk_guid, name)


def _seal(header: bytearray) -> None:
    """Recompute the header CRC in place."""
    struct.pack_into('<I', header, 16, 0)
    size = struct.unpack_from('<I', header, 12)[0]
    struct.pack_into('<I', header, 16, zlib.crc32(bytes(header[:size])) & 0xFFFFFFFF)


def build(donor_blob: bytes, old: Gpt, disk_sectors: int) -> tuple[bytes, bytes, dict]:
    """Return (gpt-primary.bin, gpt-backup.bin, layout dict) for a disk of `disk_sectors`."""
    donor = parse_gpt(donor_blob, 'donor GPT')
    last = disk_sectors - 1
    last_usable = last - RESERVED_TAIL
    header = bytearray(donor_blob[SECTOR:2 * SECTOR])
    entries = bytearray(donor.entries)
    names = [p.name for p in donor.partitions]
    if names[-1] not in ('UDISK', 'userdata') or len(set(names)) != len(names):
        raise ValueError(f'donor GPT: last partition must be UDISK/userdata and names unique, got {names}')
    parts = []
    for p in donor.partitions:
        at = p.index * donor.entry_size
        name, end = p.name, p.end_lba
        if p is donor.partitions[-1]:
            name, end = 'userdata', last_usable
            entries[at + 56:at + 128] = name.encode('utf-16le').ljust(72, b'\0')
            struct.pack_into('<Q', entries, at + 40, end)
        entries[at + 16:at + 32] = partition_guid(old.disk_guid, name).bytes_le
        parts.append({'name': name, 'start_lba': p.start_lba, 'end_lba': end,
                      'bytes': (end - p.start_lba + 1) * SECTOR})
    for a, b in zip(parts, parts[1:], strict=False):
        if a['end_lba'] >= b['start_lba']:
            raise ValueError(f'partitions overlap: {a["name"]} / {b["name"]}')
    if parts[0]['start_lba'] < donor.first_usable or parts[-1]['end_lba'] < parts[-1]['start_lba']:
        raise ValueError('disk is too small for the donor layout')
    header[56:72] = old.disk_guid.bytes_le
    struct.pack_into('<QQ', header, 24, 1, last)
    struct.pack_into('<Q', header, 48, last_usable)
    struct.pack_into('<I', header, 88, zlib.crc32(bytes(entries)) & 0xFFFFFFFF)
    _seal(header)

    mbr = bytearray(donor_blob[:SECTOR])
    struct.pack_into('<I', mbr, 446 + 12, min(last, 0xFFFFFFFF))  # protective partition size
    primary = bytearray(PRIMARY_SECTORS * SECTOR)
    end_of_entries = donor.entries_lba * SECTOR + len(entries)
    if end_of_entries > len(primary):
        raise ValueError('donor GPT entries do not fit before LBA 16')
    primary[:SECTOR] = mbr
    primary[SECTOR:2 * SECTOR] = header
    primary[donor.entries_lba * SECTOR:end_of_entries] = entries

    sectors = donor.entry_sectors
    backup_header = bytearray(header)
    struct.pack_into('<QQ', backup_header, 24, last, 1)
    struct.pack_into('<Q', backup_header, 72, last - sectors)
    _seal(backup_header)
    backup = bytes(entries).ljust(sectors * SECTOR, b'\0') + bytes(backup_header)
    layout = {'disk_sectors': disk_sectors, 'backup_start_lba': last - sectors, 'partitions': parts}
    return bytes(primary), backup, layout


def check_backup_gpt(backup_blob: bytes, disk_sectors: int) -> Gpt:
    """Parse and CRC-check a gpt-backup.bin made by build() for a disk of `disk_sectors`."""
    hdr = backup_blob[-SECTOR:]
    size, my, alt, first, last, elba, count, esize, ecrc = parse_header(hdr, 'gpt-backup.bin')
    if my != disk_sectors - 1 or alt != 1 or elba != disk_sectors - len(backup_blob) // SECTOR:
        raise ValueError('gpt-backup.bin: header LBAs do not fit this disk')
    parts = parse_entries(backup_blob[:-SECTOR], count, esize, ecrc, 'gpt-backup.bin')
    return Gpt(bytes(hdr[:size]), my, alt, first, last, uuid.UUID(bytes_le=bytes(hdr[56:72])),
               elba, count, esize, backup_blob[:count * esize], parts)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while b := f.read(1 << 22):
            h.update(b)
    return h.hexdigest()


def load_backup_head(backup: Path, sectors: int = 34) -> tuple[bytes, dict]:
    """Return (first `sectors` sectors of the user area, info.json) from a backup_emmc.py backup."""
    info = json.loads((backup / 'info.json').read_text())
    chunk = backup / 'chunk-00.bin'
    manifest = backup / 'manifest.json'
    if manifest.is_file():
        entry = {m['file']: m for m in json.loads(manifest.read_text())}.get(chunk.name)
        if entry is None or sha256_file(chunk) != entry['sha256']:
            raise SystemExit(f'{chunk}: missing from manifest.json or SHA-256 mismatch')
    with chunk.open('rb') as f:
        head = f.read(sectors * SECTOR)
    return head, info


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--donor', type=Path, required=True, help='sunxi_gpt.fex from the Lemon image')
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument('--backup', type=Path, help='backup_emmc.py folder (info.json + chunk-00.bin)')
    src.add_argument('--old-gpt', type=Path, metavar='FILE',
                     help='raw dump of the first sectors of the user area (tests / legacy)')
    p.add_argument('--out', type=Path, required=True, help='output folder (WORKDIR)')
    a = p.parse_args(argv)

    if a.backup:
        head, info = load_backup_head(a.backup)
        disk_sectors = int(info['sectors'])
    else:
        head, info = a.old_gpt.read_bytes()[:34 * SECTOR], None
    try:
        old = parse_gpt(head, 'old GPT')
        if info is None:
            disk_sectors = old.alternate_lba + 1
        elif old.alternate_lba != disk_sectors - 1:
            print(f'NOTE: old GPT backup header LBA {old.alternate_lba} is not the last sector '
                  f'({disk_sectors - 1}); using the eMMC size from info.json')
        primary, backup, layout = build(a.donor.read_bytes(), old, disk_sectors)
        # Self-check: both copies parse, CRCs hold, and they describe the same table.
        new = parse_gpt(primary, 'new primary GPT')
        bak = check_backup_gpt(backup, disk_sectors)
        if new.entries != bak.entries or new.disk_guid != old.disk_guid:
            raise ValueError('internal error: primary and backup GPT differ')
    except ValueError as exc:
        raise SystemExit(f'ERROR: {exc}') from None
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / 'gpt-primary.bin').write_bytes(primary)
    (a.out / 'gpt-backup.bin').write_bytes(backup)
    (a.out / 'layout.json').write_text(json.dumps(layout, indent=2) + '\n')
    user = layout['partitions'][-1]
    print(f'old GPT: {len(old.partitions)} partitions, disk GUID kept')
    print(f'new GPT: {len(layout["partitions"])} partitions for {disk_sectors} sectors; '
          f'userdata {user["start_lba"]}-{user["end_lba"]} ({user["bytes"]} bytes); '
          f'backup GPT at LBA {layout["backup_start_lba"]}')
    print(f'wrote {a.out / "gpt-primary.bin"}, gpt-backup.bin, layout.json')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
