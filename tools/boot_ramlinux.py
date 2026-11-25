#!/usr/bin/env python3
"""Start the S3 RAM Linux on a box in FEL mode. Loads everything into RAM; never writes the eMMC.

    boot_ramlinux.py --fel PATH_TO_sunxi-fel --dir RAMLINUX_OUT [--dry-run]

RAMLINUX_OUT is the output folder of ramlinux/build.sh. Its SHA256SUMS file is checked first.
Then one sunxi-fel call runs the SPL (DRAM init), loads the kernel, initramfs and device tree,
turns the watchdog off and starts U-Boot, which boots Linux from RAM:

    sunxi-fel -v uboot s3-ramlinux-loader.bin
              write 0x42000000 Image.gz
              write 0x48000000 initramfs.cpio.gz
              write 0x4c000000 s3-v2-linux.dtb
              writel 0x030090b8 0

After about 20 s the box shows a USB serial console (device_shell.py) and a USB network
(box 192.168.77.1, host 192.168.77.10 by DHCP). --dry-run checks the files and prints the command.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

LOADS = [('0x42000000', 'Image.gz', 0x06000000),            # up to the initramfs address
         ('0x48000000', 'initramfs.cpio.gz', 0x04000000),   # up to the DTB address
         ('0x4c000000', 's3-v2-linux.dtb', 0x00100000)]
LOADER = 's3-ramlinux-loader.bin'
WATCHDOG_MODE_REG = '0x030090b8'


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while b := f.read(1 << 22):
            h.update(b)
    return h.hexdigest()


def verify_dir(out: Path) -> None:
    """Check SHA256SUMS, file sizes and that the loader's initrd size equals initramfs.cpio.gz."""
    sums_file = out / 'SHA256SUMS'
    if not sums_file.is_file():
        raise SystemExit(f'{sums_file} missing; run ramlinux/build.sh first')
    sums = {}
    for line in sums_file.read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip('*')] = digest.lower()
    for name in [LOADER] + [n for _, n, _ in LOADS]:
        path = out / name
        if name not in sums:
            raise SystemExit(f'{name} is not listed in SHA256SUMS')
        if not path.is_file():
            raise SystemExit(f'{path} missing')
        if sha256_file(path) != sums[name]:
            raise SystemExit(f'{name}: SHA-256 does not match SHA256SUMS; rebuild with ramlinux/build.sh')
        print(f'ok  {name}  {sums[name][:16]}...')
    for _, name, limit in LOADS:
        if (out / name).stat().st_size > limit:
            raise SystemExit(f'{name} is larger than its {limit // (1 << 20)} MiB load window')
    bootcmd = out / 'bootcommand.txt'
    if bootcmd.is_file():  # U-Boot is built with the exact initrd size; a rebuilt initramfs needs a new loader
        m = re.search(r'0x48000000:([0-9a-fA-F]+)', bootcmd.read_text())
        size = (out / 'initramfs.cpio.gz').stat().st_size
        if m and int(m.group(1), 16) != size:
            raise SystemExit(f'loader expects an initramfs of {int(m.group(1), 16)} bytes, file has {size}; '
                             'rebuild everything with ramlinux/build.sh')


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--fel', default='sunxi-fel', metavar='PATH', help='sunxi-fel binary (default: sunxi-fel in PATH)')
    p.add_argument('--dir', type=Path, required=True, metavar='RAMLINUX_OUT', help='output folder of ramlinux/build.sh')
    p.add_argument('--dry-run', action='store_true', help='check files and print the command only')
    a = p.parse_args(argv)

    verify_dir(a.dir)
    cmd = [a.fel, '-v', 'uboot', str(a.dir / LOADER)]
    for addr, name, _ in LOADS:
        cmd += ['write', addr, str(a.dir / name)]
    cmd += ['writel', WATCHDOG_MODE_REG, '0']
    print(shlex.join(cmd))
    if a.dry_run:
        print('dry run: nothing sent to the box')
        return 0
    print('Uploading the RAM Linux over FEL (about 1 minute). The eMMC is not written.', flush=True)
    try:
        r = subprocess.run(cmd, timeout=600)
    except FileNotFoundError:
        raise SystemExit(f'sunxi-fel not found: {a.fel}') from None
    except subprocess.TimeoutExpired:
        raise SystemExit('sunxi-fel did not finish in 10 minutes') from None
    if r.returncode:
        raise SystemExit(f'sunxi-fel failed with exit status {r.returncode}; is the box in FEL mode?')
    print('Started. Wait about 20 s, then use tools/device_shell.py or open http://192.168.77.1/.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
