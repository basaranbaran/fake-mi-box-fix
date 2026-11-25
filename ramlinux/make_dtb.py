#!/usr/bin/env python3
"""Turn Armbian's Orange Pi Zero3 DTB into the S3_V2.0 RAM Linux DTB.

Usage: make_dtb.py <sun50i-h618-orangepi-zero3.dtb> <output.dtb>
Needs fdtget/fdtput (device-tree-compiler). Edits only the copy.
"""
import shutil
import subprocess
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
shutil.copy2(src, out)


def get(node: str, prop: str, typ: str = 's') -> str:
    return subprocess.check_output(['fdtget', '-t', typ, str(out), node, prop], text=True).strip()


def put(node: str, prop: str, *vals: object, typ: str = 's') -> None:
    subprocess.run(['fdtput', '-t', typ, str(out), node, prop, *map(str, vals)], check=True)


def flag(node: str, prop: str) -> None:
    subprocess.run(['fdtput', str(out), node, prop], check=True)


def delete(node: str, prop: str, must: bool = True) -> None:
    subprocess.run(['fdtput', '-d', str(out), node, prop], check=must,
                   stderr=None if must else subprocess.DEVNULL)


def path(label: str) -> str:
    return get('/__symbols__', label)


def phandle(label: str) -> str:
    return get(path(label), 'phandle', 'x')


put('/', 'model', 'S3 V2 custom Linux')
put('/', 'compatible', 'custom,s3-v2', 'allwinner,sun50i-h618', 'allwinner,sun50i-h616')

# Only the 8-bit eMMC on mmc2; no SD card slot, SDIO Wi-Fi left off.
put(path('mmc0'), 'status', 'disabled')
put(path('mmc1'), 'status', 'disabled')
emmc = path('mmc2')
put(emmc, 'compatible', 'allwinner,sun50i-h616-emmc', 'allwinner,sun50i-a100-emmc', 'allwinner,sun50i-h5-emmc')
put(emmc, 'status', 'okay')
put(emmc, 'bus-width', '8', typ='x')
put(emmc, 'max-frequency', 25000000, typ='u')
flag(emmc, 'non-removable')
flag(emmc, 'cap-mmc-hw-reset')
delete(emmc, 'mmc-ddr-3_3v')
delete(emmc, 'mmc-ddr-1_8v', must=False)
put(emmc, 'vmmc-supply', phandle('reg_dldo1'), typ='x')
put(emmc, 'vqmmc-supply', phandle('reg_aldo1'), typ='x')
put(path('pio'), 'vcc-pc-supply', phandle('reg_aldo1'), typ='x')
put(path('reg_dcdc3'), 'regulator-min-microvolt', 1200000, typ='u')
put(path('reg_dcdc3'), 'regulator-max-microvolt', 1200000, typ='u')

for label in ['ehci0', 'ohci0', 'emac0', 'watchdog', 'spi0', 'spi1']:
    try:
        put(path(label), 'status', 'disabled')
    except subprocess.CalledProcessError:
        pass
put('/leds', 'status', 'disabled')

# The board's USB-A socket is the OTG port: run it as a USB gadget towards the host computer.
put(path('usbotg'), 'status', 'okay')
put(path('usbotg'), 'dr_mode', 'peripheral')
# Orange Pi's PC16-controlled USB1 regulator does not exist on this TV box.
# Leaving it referenced keeps the USB PHY and the gadget controller from probing.
delete(path('usbphy'), 'usb1_vbus-supply')
put('/regulator-usb1-vbus', 'status', 'disabled')

put('/chosen', 'bootargs',
    'console=ttyS0,115200 earlycon=uart8250,mmio32,0x05000000 rdinit=/init nokaslr panic=0 '
    'loglevel=8 ignore_loglevel clk_ignore_unused regulator_ignore_unused')
# Keep 1 MiB at 0x4d000000 out of Linux's hands (U-Boot writes a boot marker there).
subprocess.run(['fdtput', '-c', str(out), '/reserved-memory/s3-trace@4d000000'], check=True)
put('/reserved-memory/s3-trace@4d000000', 'reg', '0', '4d000000', '0', '100000', typ='x')
flag('/reserved-memory/s3-trace@4d000000', 'no-map')
print('DTB ready:', out)
