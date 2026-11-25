#!/usr/bin/env python3
"""Install the Lemon TV Mini H618 firmware on the S3_V2.0 box, in two stages. Dry run by default.

    install.py boot    --backup BACKUPDIR --files WORKDIR [--host-ip 192.168.77.10] [--port PORT] [--write]
    install.py android --backup BACKUPDIR --files WORKDIR [--host-ip 192.168.77.10] [--port PORT] [--write]

BACKUPDIR  complete backup from backup_emmc.py (info.json, manifest.json, boot0/boot1, chunk-NN.bin)
WORKDIR    imagewty.py android output plus toc0-s3.fex (patch_toc0_dram.py) and
           gpt-primary.bin, gpt-backup.bin, layout.json (prepare_gpt.py)

Without --write the tool runs every offline pre-check and prints the exact device commands. It
does not touch the box. With --write it needs the RAM Linux running: it serves the files over HTTP
on HOST-IP:18780 and types each command into the USB console (device_shell.py). Every write is
read back on the box and compared by SHA-256. The tool stops at the first failed step.

boot stage    TOC0 (toc0-s3.fex) -> boot0 offset 0 (force_ro 0, write, force_ro 1),
              user area LBA 16 and LBA 256; TOC1 (toc1.fex) -> user area LBA 32800.
              Partitions and GPT are not touched. Reboot afterwards: a logo must appear.
android stage the Android partitions from WORKDIR/manifest.json into the new layout; frp, private,
              media_data and Reserve0 keep their content (copied from the old partitions);
              metadata is zeroed fully, customize and userdata in their first 4 MiB;
              then the backup GPT and, last of all, the primary GPT.

Device pre-checks: eMMC name and sector count equal info.json; the primary GPT on the eMMC equals
the backup (android: refuses when the Lemon GPT is already there); android: the boot stage is done.

S3_SYS / S3_DEV (default /sys, /dev) change the device paths in the commands. They exist for
tests only, like in ramlinux/rootfs/s3backup.cgi.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import backup_emmc
import device_shell
import prepare_gpt
import toc0
from patch_toc0_dram import S3_TOC0_SHA256, read_dram_fields

SECTOR = 512
MiB = 1 << 20
GiB = 1 << 30
HTTP_PORT = 18780
TOC0_LBAS = (16, 256)
TOC1_LBA = 32800
LEMON_TOC1_SHA256 = 'a81a0b2975322aa746ab4a0e7218fe91a6f7441d0b4ec6afe26204c88d81d18d'
PRESERVE = ('frp', 'private', 'media_data', 'Reserve0')
ZERO = {'metadata': None, 'customize': 4 * MiB, 'userdata': 4 * MiB}  # None = whole partition

SYS = os.environ.get('S3_SYS', '/sys')
DEV = os.environ.get('S3_DEV', '/dev')
DISK = f'{DEV}/mmcblk1'
BOOT0 = f'{DEV}/mmcblk1boot0'
FORCE_RO = f'{SYS}/block/mmcblk1boot0/force_ro'
SYSBLK = f'{SYS}/class/block/mmcblk1'
DROP_CACHES = 'echo 3 2>/dev/null > /proc/sys/vm/drop_caches || true'  # read back from the eMMC, not RAM


class Refused(Exception):
    """A pre-check or a device step failed. Nothing after it runs."""


@dataclass
class Step:
    """One command typed into the box console."""
    title: str
    command: str
    timeout: float = 60
    expect: str = ''                                    # marker line the output must contain
    refuse: dict[int, str] = field(default_factory=dict)  # exit status -> reason to stop
    after: Callable[[str], None] | None = None
    hint: str = ''                                      # added to the message when the step fails


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    serve: dict[str, Path | bytes] = field(default_factory=dict)
    final: str = ''
    checks: int = 0   # the first `checks` steps only read the eMMC
    done: int = 0     # steps finished by execute()


# ---------------------------------------------------------------- helpers

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while b := f.read(4 * MiB):
            h.update(b)
    return h.hexdigest()


def ok(text: str) -> None:
    print(f'  ok  {text}')


def require(cond: bool, text: str) -> None:
    if not cond:
        raise Refused(text)


def sha_cmd(path: str, start: int, sectors: int) -> str:
    """Shell pipeline that prints the SHA-256 of `sectors` sectors of `path` from LBA `start`."""
    if start % 8 == 0 and sectors % 8 == 0:
        dd = f'dd if={path} bs=4096 skip={start // 8} count={sectors // 8} 2>/dev/null'
    else:
        dd = f'dd if={path} bs=512 skip={start} count={sectors} 2>/dev/null'
    return f'{dd} | sha256sum | cut -d" " -f1'


def has_marker(output: str, marker: str) -> re.Match[str] | None:
    """Find `marker` (optionally `marker=value`) as a whole output line, after any shell prompt.

    A line-anchored match ignores a command line that the console might echo back.
    """
    return re.search(rf'(?m)^(?:\S*\s?#\s)*{re.escape(marker)}(?:=(\w+))?\s*$', output.replace('\r', ''))


def backup_range(backup: Path, info: dict, start_lba: int, nbytes: int) -> bytes:
    """Read `nbytes` from the user area at `start_lba` out of the backup chunk files."""
    chunk = int(info['chunk_mib']) * MiB
    pos, out = start_lba * SECTOR, bytearray()
    while len(out) < nbytes:
        n, off = divmod(pos, chunk)
        with (backup / f'chunk-{n:02d}.bin').open('rb') as f:
            f.seek(off)
            b = f.read(min(nbytes - len(out), chunk - off))
        if not b:
            raise Refused('backup chunk files are shorter than info.json says')
        out += b
        pos += len(b)
    return bytes(out)


def overlaps(a: tuple[str, int, int], b: tuple[str, int, int]) -> bool:
    return a[1] <= b[2] and b[1] <= a[2]


# ---------------------------------------------------------------- offline checks

@dataclass
class Facts:
    backup: Path
    info: dict
    old: prepare_gpt.Gpt
    old_gpt_sha: str
    boot0_sha: str
    toc0: Path
    toc1: Path
    toc0_sha: str
    toc1_sha: str


def check_backup(backup: Path) -> tuple[dict, prepare_gpt.Gpt, str, str]:
    """Backup is complete and every file matches manifest.json. Returns info, old GPT, GPT sha, boot0 sha."""
    print(f'Backup {backup}')
    try:
        info = json.loads((backup / 'info.json').read_text())
        manifest = {m['file']: m for m in json.loads((backup / 'manifest.json').read_text())}
    except (OSError, ValueError, KeyError) as exc:
        raise Refused(f'backup folder is not a backup_emmc.py backup: {exc}') from None
    items = backup_emmc.plan(info)
    for item in items:
        path, m = backup / item['file'], manifest.get(item['file'])
        require(m is not None and m['bytes'] == item['bytes'], f'{item["file"]} missing from manifest.json; '
                'finish the backup with backup_emmc.py first')
        require(path.is_file() and path.stat().st_size == item['bytes'], f'{path} missing or wrong size')
        require(sha256_file(path) == m['sha256'], f'{path}: SHA-256 does not match manifest.json')
    ok(f'backup complete: {len(items)} files, {sum(i["bytes"] for i in items) // MiB} MiB, every SHA-256 matches')
    ok(f'eMMC in the backup: {info["name"]}, {info["sectors"]} sectors')
    head = backup_range(backup, info, 0, 34 * SECTOR)
    try:
        old = prepare_gpt.parse_gpt(head, 'GPT in the backup')
    except ValueError as exc:
        raise Refused(str(exc)) from None
    old_sha = hashlib.sha256(head[:old.primary_region_sectors * SECTOR]).hexdigest()
    ok(f'old GPT in the backup: {len(old.partitions)} partitions, CRCs valid, '
       f'LBA 0-{old.primary_region_sectors - 1}')
    return info, old, old_sha, manifest['boot0.bin']['sha256']


def check_boot_files(files: Path, info: dict) -> tuple[Path, Path, str, str]:
    """TOC0 and TOC1 pass their offline checks and fit their places."""
    print(f'Boot files in {files}')
    t0, t1 = files / 'toc0-s3.fex', files / 'toc1.fex'
    for f in (t0, t1):
        require(f.is_file(), f'{f} missing (see imagewty.py android and patch_toc0_dram.py)')
        require(f.stat().st_size % SECTOR == 0, f'{f} size is not a multiple of 512')
    try:
        r0, r1 = toc0.inspect(t0), toc0.inspect_toc1(t1)
    except toc0.Toc0Error as exc:
        raise Refused(str(exc)) from None
    require(toc0.is_valid(r0), f'{t0}: checksum / firmware hash / signature check failed')
    require(r1['magic_valid'] and r1['checksum_valid'], f'{t1}: TOC1 magic or checksum invalid')
    dram = read_dram_fields(t0.read_bytes())
    ok(f'toc0-s3.fex valid, sha256 {r0["sha256"][:16]}... '
       f'({"the file tested on the box" if r0["sha256"] == S3_TOC0_SHA256 else "NOT the tested file"}); '
       f'DRAM {dram["dram_clk"]} MHz type {dram["dram_type"]} odt {dram["dx_odt"]:#x} '
       f'dri {dram["dx_dri"]:#x} tpr10 {dram["tpr10"]:#x}')
    require(dram['dram_clk'] != 720, 'toc0-s3.fex still has the Lemon 720 MHz DRAM clock; run patch_toc0_dram.py')
    ok(f'toc1.fex valid, sha256 {r1["sha256"][:16]}... '
       f'({"the file tested on the box" if r1["sha256"] == LEMON_TOC1_SHA256 else "NOT the tested file"})')
    require(t0.stat().st_size <= int(info['boot0_bytes']), 'TOC0 is larger than eMMC boot0')
    return t0, t1, r0['sha256'], r1['sha256']


def toc_areas(t0: Path, t1: Path) -> list[tuple[str, int, int]]:
    n0, n1 = t0.stat().st_size // SECTOR, t1.stat().st_size // SECTOR
    areas = [(f'TOC0 at LBA {lba}', lba, lba + n0 - 1) for lba in TOC0_LBAS]
    return areas + [(f'TOC1 at LBA {TOC1_LBA}', TOC1_LBA, TOC1_LBA + n1 - 1)]


def gpt_areas(gpt: prepare_gpt.Gpt, label: str, primary_sectors: int | None = None) -> list[tuple[str, int, int]]:
    first = [(f'{label} primary GPT', 0, (primary_sectors or gpt.primary_region_sectors) - 1),
             (f'{label} backup GPT', gpt.last_usable + 1, gpt.alternate_lba)]
    return first + [(f'{label} partition {p.name}', p.start_lba, p.end_lba) for p in gpt.partitions]


def check_toc_ranges(tocs: list[tuple[str, int, int]], others: list[tuple[str, int, int]], what: str) -> None:
    for i, a in enumerate(tocs):
        for b in tocs[i + 1:] + others:
            require(not overlaps(a, b), f'{a[0]} (LBA {a[1]}-{a[2]}) overlaps {b[0]} (LBA {b[1]}-{b[2]})')
    ranges = ', '.join(f'{a[1]}-{a[2]}' for a in tocs)
    ok(f'TOC ranges (LBA {ranges}) overlap neither each other nor {what}')


def load_facts(backup: Path, files: Path) -> Facts:
    info, old, old_sha, boot0_sha = check_backup(backup)
    t0, t1, s0, s1 = check_boot_files(files, info)
    return Facts(backup, info, old, old_sha, boot0_sha, t0, t1, s0, s1)


# ---------------------------------------------------------------- device steps

def common_steps(f: Facts) -> list[Step]:
    return [Step('eMMC on the box is the one in the backup',
                 f'test "$(cat {SYSBLK}/device/name)" = {f.info["name"]} && '
                 f'test "$(cat {SYSBLK}/size)" = {f.info["sectors"]} && echo EMMC_MATCHES_BACKUP',
                 30, 'EMMC_MATCHES_BACKUP',
                 hint='This eMMC is not the one in BACKUPDIR (name or size differs). Use the backup of this box.')]


def write_verify(src: str, dst: str, lba: int, sectors: int, sha: str, tag: str) -> str:
    """Command: copy a verified /tmp file to dst at lba, sync, read back and compare."""
    return (f'( set -e; dd if={src} of={dst} bs=512 seek={lba} conv=notrunc 2>/dev/null; sync; {DROP_CACHES}; '
            f'test "$({sha_cmd(dst, lba, sectors)})" = {sha}; echo VERIFIED_{tag} )')


def download(url: str, dst: str, sha: str, tag: str) -> str:
    return (f'( set -e; wget -q -T 60 -O {dst} {url}; '
            f'test "$(sha256sum {dst} | cut -d" " -f1)" = {sha}; echo VERIFIED_{tag} )')


def plan_boot(f: Facts, files: Path, url: str) -> Plan:
    n0, n1 = f.toc0.stat().st_size // SECTOR, f.toc1.stat().st_size // SECTOR
    others = gpt_areas(f.old, 'old')
    what = 'the old GPT nor any old partition'
    layout = files / 'layout.json'
    if (files / 'gpt-primary.bin').is_file() and layout.is_file():
        new = prepare_gpt.parse_gpt((files / 'gpt-primary.bin').read_bytes(), 'gpt-primary.bin')
        others += gpt_areas(new, 'new', prepare_gpt.PRIMARY_SECTORS)
        what += ' nor the new GPT / partitions'
    check_toc_ranges(toc_areas(f.toc0, f.toc1), others, what)
    plan = Plan(serve={'toc0': f.toc0, 'toc1': f.toc1})
    # Only the TOC0 area of boot0 changes; the rest must still equal the backup. This also lets a
    # second run continue after an interrupted first run.
    tail_sha = hashlib.sha256((f.backup / 'boot0.bin').read_bytes()[n0 * SECTOR:]).hexdigest()
    plan.steps = common_steps(f) + [
        Step('primary GPT on the eMMC equals the backup',
             f'test "$({sha_cmd(DISK, 0, f.old.primary_region_sectors)})" = {f.old_gpt_sha} && echo GPT_MATCHES_BACKUP',
             30, 'GPT_MATCHES_BACKUP',
             hint='The GPT on the eMMC differs from the backup: the android stage was run already, or the '
                  'eMMC changed after the backup. Make a new backup before you continue.'),
        Step(f'boot0 after the TOC0 area (from byte {n0 * SECTOR}) equals the backup',
             f'test "$(dd if={BOOT0} bs=512 skip={n0} 2>/dev/null | sha256sum | cut -d" " -f1)" = {tail_sha} '
             f'&& echo BOOT0_MATCHES_BACKUP',
             60, 'BOOT0_MATCHES_BACKUP', hint='boot0 changed after the backup. Make a new backup first.'),
        Step('download TOC0 into box RAM', download(f'{url}/toc0', '/tmp/s3-toc0.bin', f.toc0_sha, 'download_toc0'),
             120, 'VERIFIED_download_toc0'),
        Step('download TOC1 into box RAM', download(f'{url}/toc1', '/tmp/s3-toc1.bin', f.toc1_sha, 'download_toc1'),
             120, 'VERIFIED_download_toc1'),
        Step('write TOC0 to eMMC boot0 offset 0 (force_ro 0, write, force_ro 1)',
             f'( echo 0 > {FORCE_RO} || exit 1; dd if=/tmp/s3-toc0.bin of={BOOT0} bs=512 conv=notrunc 2>/dev/null; '
             f'rc=$?; sync; echo 1 > {FORCE_RO}; test $rc = 0 || exit 1; {DROP_CACHES}; '
             f'test "$({sha_cmd(BOOT0, 0, n0)})" = {f.toc0_sha} && echo VERIFIED_boot0 )',
             60, 'VERIFIED_boot0'),
        *[Step(f'write TOC0 to user area LBA {lba}',
               write_verify('/tmp/s3-toc0.bin', DISK, lba, n0, f.toc0_sha, f'lba{lba}'), 60, f'VERIFIED_lba{lba}')
          for lba in TOC0_LBAS],
        Step(f'write TOC1 to user area LBA {TOC1_LBA}',
             write_verify('/tmp/s3-toc1.bin', DISK, TOC1_LBA, n1, f.toc1_sha, f'lba{TOC1_LBA}'),
             60, f'VERIFIED_lba{TOC1_LBA}'),
    ]
    plan.checks = 3
    plan.final = ('BOOT_STAGE_VERIFIED. Next: power the box off and on with HDMI connected; a logo must '
                  'appear. Then enter FEL again, start the RAM Linux and run the android stage.')
    return plan


def plan_android(f: Facts, files: Path, url: str) -> Plan:
    sectors = int(f.info['sectors'])
    print(f'Android files in {files}')
    try:
        manifest = json.loads((files / 'manifest.json').read_text())
        layout = json.loads((files / 'layout.json').read_text())
        primary = (files / 'gpt-primary.bin').read_bytes()
        backup_gpt = (files / 'gpt-backup.bin').read_bytes()
        new = prepare_gpt.parse_gpt(primary, 'gpt-primary.bin')
        bak = prepare_gpt.check_backup_gpt(backup_gpt, sectors)
    except (OSError, ValueError, KeyError) as exc:
        raise Refused(f'WORKDIR is not complete (imagewty.py android + prepare_gpt.py): {exc}') from None
    require(len(primary) % SECTOR == 0 and len(primary) <= prepare_gpt.PRIMARY_SECTORS * SECTOR,
            'gpt-primary.bin must be at most 16 sectors (TOC0 starts at LBA 16)')
    require(new.alternate_lba == sectors - 1 and layout['disk_sectors'] == sectors,
            f'new GPT is for another disk size (backup says {sectors} sectors)')
    require(new.entries == bak.entries and new.disk_guid == bak.disk_guid, 'gpt-primary.bin and gpt-backup.bin differ')
    require(new.disk_guid == f.old.disk_guid, 'new GPT was not prepared from this backup (disk GUID differs); '
            'run prepare_gpt.py --backup BACKUPDIR again')
    require(layout['backup_start_lba'] == sectors - len(backup_gpt) // SECTOR, 'layout.json backup_start_lba is wrong')
    require([(p.name, p.start_lba, p.end_lba) for p in new.partitions]
            == [(p['name'], p['start_lba'], p['end_lba']) for p in layout['partitions']],
            'layout.json does not match gpt-primary.bin')
    ok(f'new GPT valid: {len(new.partitions)} partitions, same disk GUID as the backup, backup GPT at LBA '
       f'{layout["backup_start_lba"]}')
    targets = new.by_name()
    parts = []
    for m in manifest:
        path, t = files / m['file'], targets.get(m['partition'])
        require(t is not None, f'partition {m["partition"]} is not in the new GPT')
        require(path.is_file() and path.stat().st_size == m['bytes'], f'{path} missing or wrong size')
        require(m['bytes'] <= t.bytes, f'{m["file"]} ({m["bytes"]} bytes) does not fit {t.name} ({t.bytes} bytes)')
        require(t.start_lba % 8 == 0 and m['bytes'] % SECTOR == 0, f'{t.name}: alignment check failed')
        require(sha256_file(path) == m['sha256'], f'{path}: SHA-256 does not match manifest.json')
        parts.append((m, t))
    ok(f'{len(parts)} partition files present, SHA-256 match manifest.json, each fits its partition')
    old = f.old.by_name()
    keep = {}
    for name in PRESERVE:
        require(name in old and name in targets, f'{name} missing in the old or new GPT')
        require(targets[name].bytes >= old[name].bytes, f'new {name} is smaller than the old one')
        data = backup_range(f.backup, f.info, old[name].start_lba, old[name].bytes)
        keep[name] = (old[name], targets[name], hashlib.sha256(data).hexdigest(), data)
    ok(f'kept partitions {", ".join(PRESERVE)} exist in both tables and fit')
    for name in ZERO:
        require(name in targets, f'{name} missing in the new GPT')
    check_toc_ranges(toc_areas(f.toc0, f.toc1), gpt_areas(new, 'new', prepare_gpt.PRIMARY_SECTORS),
                     'the new GPT nor any new partition')
    n0, n1 = f.toc0.stat().st_size // SECTOR, f.toc1.stat().st_size // SECTOR
    new_sha = hashlib.sha256(primary[:new.primary_region_sectors * SECTOR]).hexdigest()

    plan = Plan(serve={m['partition']: files / m['file'] for m, _ in parts})
    plan.serve.update({'gpt-primary': files / 'gpt-primary.bin', 'gpt-backup': files / 'gpt-backup.bin'})
    plan.serve.update({f'keep-{name}': k[3] for name, k in keep.items()})
    steps = common_steps(f) + [
        Step('primary GPT on the eMMC equals the backup (not yet the Lemon GPT)',
             f'( a=$({sha_cmd(DISK, 0, f.old.primary_region_sectors)}); '
             f'b=$({sha_cmd(DISK, 0, new.primary_region_sectors)}); '
             f'[ "$b" = {new_sha} ] && {{ echo GPT_STATE=lemon; exit 3; }}; '
             f'[ "$a" = {f.old_gpt_sha} ] && echo GPT_STATE=original )',
             60, 'GPT_STATE', {3: 'the box already has the Lemon GPT: the android stage was done before.'},
             hint='The GPT on the eMMC is neither the backed-up one nor the Lemon one. Make a new backup first.'),
        Step('boot stage is done (TOC0 in boot0, LBA 16, LBA 256; TOC1 at LBA 32800)',
             f'test "$({sha_cmd(BOOT0, 0, n0)})" = {f.toc0_sha} && '
             f'test "$({sha_cmd(DISK, TOC0_LBAS[0], n0)})" = {f.toc0_sha} && '
             f'test "$({sha_cmd(DISK, TOC0_LBAS[1], n0)})" = {f.toc0_sha} && '
             f'test "$({sha_cmd(DISK, TOC1_LBA, n1)})" = {f.toc1_sha} && echo BOOT_STAGE_DONE',
             60, 'BOOT_STAGE_DONE',
             hint='Run the boot stage first (install.py boot), then check that the box shows a logo.'),
    ]
    for name, (o, _t, sha, _data) in keep.items():
        tmp = f'/tmp/s3-keep-{name}.bin'
        steps.append(Step(
            f'copy old {name} (LBA {o.start_lba}, {o.bytes} bytes) into box RAM',
            f'( set -e; dd if={DISK} of={tmp} bs=512 skip={o.start_lba} count={o.bytes // SECTOR} 2>/dev/null; '
            f'if [ "$(sha256sum {tmp} | cut -d" " -f1)" = {sha} ]; then echo KEEP_SOURCE=device; else '
            f'wget -q -T 60 -O {tmp} {url}/keep-{name}; test "$(sha256sum {tmp} | cut -d" " -f1)" = {sha}; '
            f'echo KEEP_SOURCE=backup; fi )',
            90, 'KEEP_SOURCE', after=_keep_note(name)))
    for m, t in parts:
        size = m['bytes']
        blocks, rem = divmod(size, 4096)
        reads = f'dd if={DISK} bs=4096 skip={t.start_lba // 8} count={blocks} 2>/dev/null'
        if rem:
            reads += f'; dd if={DISK} bs=512 skip={t.start_lba + blocks * 8} count={rem // SECTOR} 2>/dev/null'
        steps.append(Step(
            f'write {m["file"]} to {t.name} (LBA {t.start_lba}, {size} bytes) and read back',
            f'( set -e; set -o pipefail; wget -q -T 120 -O - {url}/{t.name} | '
            f'dd of={DISK} bs=4096 seek={t.start_lba // 8} conv=notrunc; sync; {DROP_CACHES}; '
            f'got=$( {{ {reads}; }} | sha256sum | cut -d" " -f1); test "$got" = {m["sha256"]}; echo VERIFIED_{t.name} )',
            180 if size <= GiB else max(1200, size // (3 * MiB)), f'VERIFIED_{t.name}'))
    for name, (o, t, sha, _data) in keep.items():
        tmp = f'/tmp/s3-keep-{name}.bin'
        seek = f'bs=4096 seek={t.start_lba // 8}' if t.start_lba % 8 == 0 else f'bs=512 seek={t.start_lba}'
        steps.append(Step(
            f'write kept {name} to new {name} (LBA {t.start_lba}) and read back',
            f'( set -e; dd if={tmp} of={DISK} {seek} conv=notrunc 2>/dev/null; sync; {DROP_CACHES}; '
            f'test "$({sha_cmd(DISK, t.start_lba, o.bytes // SECTOR)})" = {sha}; echo VERIFIED_keep_{name} )',
            90, f'VERIFIED_keep_{name}'))
    for name, limit in ZERO.items():
        t = targets[name]
        nbytes = t.bytes if limit is None else min(limit, t.bytes)
        zero_sha = hashlib.sha256(bytes(nbytes)).hexdigest()
        steps.append(Step(
            f'zero {name} ({"whole partition" if limit is None else "first"} {nbytes // MiB} MiB)',
            f'( set -e; dd if=/dev/zero of={DISK} bs=4096 seek={t.start_lba // 8} count={nbytes // 4096} '
            f'conv=notrunc 2>/dev/null; sync; {DROP_CACHES}; '
            f'test "$({sha_cmd(DISK, t.start_lba, nbytes // SECTOR)})" = {zero_sha}; echo VERIFIED_zero_{name} )',
            90, f'VERIFIED_zero_{name}'))
    for key, lba in (('gpt-backup', layout['backup_start_lba']), ('gpt-primary', 0)):
        blob = backup_gpt if key == 'gpt-backup' else primary
        sha = hashlib.sha256(blob).hexdigest()
        steps.append(Step(
            f'write {key}.bin at LBA {lba} and read back' + (' (last write)' if lba == 0 else ''),
            f'( set -e; wget -q -T 60 -O /tmp/{key}.bin {url}/{key}; '
            f'test "$(sha256sum /tmp/{key}.bin | cut -d" " -f1)" = {sha}; '
            f'dd if=/tmp/{key}.bin of={DISK} bs=512 seek={lba} conv=notrunc 2>/dev/null; sync; {DROP_CACHES}; '
            f'test "$({sha_cmd(DISK, lba, len(blob) // SECTOR)})" = {sha}; echo VERIFIED_{key} )',
            60, f'VERIFIED_{key}'))
    plan.steps = steps
    plan.checks = 3
    plan.final = ('ANDROID_PARTITIONS_AND_GPT_VERIFIED. Next: power the box off and on with HDMI connected. '
                  'The first Android start takes several minutes.')
    return plan


def _keep_note(name: str) -> Callable[[str], None]:
    def note(output: str) -> None:
        m = has_marker(output, 'KEEP_SOURCE')
        if m and m.group(1) == 'backup':
            print(f'      NOTE: old {name} on the eMMC differs from the backup; the backup copy is used')
    return note


# ---------------------------------------------------------------- run

def make_server(host: str, items: dict[str, Path | bytes]) -> http.server.ThreadingHTTPServer:
    """HTTP server that hands out the planned files by key (GET /<key>)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            src = items.get(self.path.lstrip('/'))
            if src is None:
                self.send_error(404)
                return
            size = len(src) if isinstance(src, bytes) else src.stat().st_size
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(size))
            self.end_headers()
            try:
                if isinstance(src, bytes):
                    self.wfile.write(src)
                    return
                sent, next_log = 0, 256 * MiB
                with src.open('rb') as f:
                    while b := f.read(MiB):
                        self.wfile.write(b)
                        sent += len(b)
                        if sent >= next_log:
                            print(f'      sent {self.path.lstrip("/")}: {sent // MiB}/{size // MiB} MiB', flush=True)
                            next_log += 256 * MiB
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args: object) -> None:
            pass

    try:
        return http.server.ThreadingHTTPServer((host, HTTP_PORT), Handler)
    except OSError as exc:
        raise Refused(f'cannot serve files on {host}:{HTTP_PORT}: {exc}. Is the USB network up '
                      f'(this computer must have {host})?') from None


def print_plan(plan: Plan, url: str) -> None:
    print(f'\nDevice steps, in order (files are served from {url}/):')
    for i, s in enumerate(plan.steps, 1):
        print(f'{i:3}. {s.title}  [timeout {s.timeout:.0f} s]')
        print(f'     $ {s.command}')


def execute(plan: Plan, port: str | None) -> None:
    for i, s in enumerate(plan.steps, 1):
        print(f'[{i}/{len(plan.steps)}] {s.title}', flush=True)
        try:
            status, output = device_shell.run(s.command, s.timeout, port)
        except device_shell.ConsoleError as exc:
            raise Refused(f'console error in step {i}: {exc}') from None
        if status in s.refuse:
            raise Refused(s.refuse[status])
        if status != 0 or (s.expect and not has_marker(output, s.expect)):
            tail = '\n'.join(line for line in output.replace('\r', '').splitlines()[-8:] if line.strip(' ~#'))
            raise Refused(f'step {i} failed (exit status {status}): {s.title}'
                          + (f'\n{tail}' if tail else '') + (f'\n{s.hint}' if s.hint else ''))
        if s.after:
            s.after(output)
        plan.done = i
        print('      done', flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('stage', choices=('boot', 'android'))
    p.add_argument('--backup', type=Path, required=True, metavar='BACKUPDIR')
    p.add_argument('--files', type=Path, required=True, metavar='WORKDIR')
    p.add_argument('--host-ip', default='192.168.77.10', help='this computer on the USB network (default 192.168.77.10)')
    p.add_argument('--port', help='console device for device_shell.py (default: auto-detect)')
    p.add_argument('--write', action='store_true', help='really write the eMMC (default: dry run)')
    a = p.parse_args(argv)
    url = f'http://{a.host_ip}:{HTTP_PORT}'
    try:
        print(f'== install.py {a.stage} ({"WRITE" if a.write else "dry run"}) ==')
        facts = load_facts(a.backup, a.files)
        plan = plan_boot(facts, a.files, url) if a.stage == 'boot' else plan_android(facts, a.files, url)
        print('All offline pre-checks passed.')
        print_plan(plan, url)
        if not a.write:
            print('\nDRY RUN: nothing was sent to the box. The device checks (steps 1-3) run first with --write.')
            return 0
        server = make_server(a.host_ip, plan.serve)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f'\nServing files on {url}. Writing now; do not unplug the box.', flush=True)
        try:
            execute(plan, a.port)
        except Refused as exc:
            if plan.done < plan.checks:
                raise Refused(f'{exc}\nNothing was written to the eMMC.') from None
            raise Refused(f'{exc}\nSteps 1-{plan.done} are complete and verified; nothing after them ran. '
                          'Find the cause and run the same command again: '
                          + ('the GPT is written last, so the android stage starts over safely.'
                             if a.stage == 'android' else 'the boot stage only rewrites the TOC areas.')) from None
        finally:
            server.shutdown()
        print(plan.final)
        return 0
    except Refused as exc:
        sys.stdout.flush()
        print(f'\nREFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
