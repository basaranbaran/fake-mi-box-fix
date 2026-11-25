# RAM Linux for the S3_V2.0

A small Linux that runs only in the RAM of the box. The guide uses it for the backup and for the install.
It gives the host computer a USB serial console and a USB network.
It does not mount the eMMC by itself. Nothing is written to the box until you run a command that writes.

The build needs Docker. It talks to no device.

## Build

Run both commands in the repository root.

```sh
docker build -t s3-ramlinux-build ramlinux
mkdir -p out
docker run --rm -v "$PWD/ramlinux:/src:ro" -v "$PWD/out:/out" s3-ramlinux-build bash /src/build.sh
```

The build downloads U-Boot, TF-A and the Armbian kernel packages. It checks the SHA-256 of both Armbian packages.

Output in `out/`:

| File | Content |
|:--|:--|
| `s3-ramlinux-loader.bin` | SPL, U-Boot and TF-A BL31 in one image. SPL starts the DRAM. |
| `Image.gz` | The Armbian kernel (`vmlinuz`), compressed with gzip. |
| `initramfs.cpio.gz` | BusyBox root file system. |
| `s3-v2-linux.dtb` | Device tree for the S3_V2.0, made from an Armbian DTB. |
| `bootcommand.txt` | The U-Boot boot command that the loader runs. |
| `SHA256SUMS`, `BUILDINFO` | Checksums of the files above. Versions of kernel, U-Boot and TF-A. |

`tools/boot_ramlinux.py` loads these files through FEL. See [tools/README.md](../tools/README.md).

## Pins

| Part | Pin |
|:--|:--|
| Build image | `ubuntu:24.04` |
| U-Boot | tag `v2026.10` |
| TF-A | commit `9799b799fb51edef36607bcd534b5510aa4156bc`, `PLAT=sun50i_h616`, `bl31`, release build, no patch |
| Armbian kernel | `linux-image-current-sunxi64` 26.8.3, kernel 6.18.44 |
| Armbian kernel SHA-256 | `d0c9253a77d1fbe94790cd1259ef0982c85aa24cfac0b334b8e63f9df5426b25` |
| Armbian DTB package | `linux-dtb-current-sunxi64` 26.8.3 |
| Armbian DTB SHA-256 | `7aa765c8ddd4ea72938c3cbd40349079dad7d7c75d0dfdb7cd5c1030c51dec74` |
| BusyBox | `busybox-static` from Ubuntu 24.04 |

Armbian can remove old packages from its pool. If the download fails, set `ARMBIAN_KERNEL=latest`.
Then the build takes the newest `linux-image-current-sunxi64` from the Armbian index.
I did not test `latest`.

```sh
docker run --rm -e ARMBIAN_KERNEL=latest -v "$PWD/ramlinux:/src:ro" -v "$PWD/out:/out" s3-ramlinux-build bash /src/build.sh
```

## How the box boots it

1. `sunxi-fel` runs the loader. SPL starts the LPDDR3 with the S3_V2.0 DRAM values.
2. `tools/boot_ramlinux.py` writes `Image.gz` to `0x42000000`, `initramfs.cpio.gz` to `0x48000000` and the DTB to `0x4c000000`.
3. U-Boot runs `bootcommand.txt`. It uses `booti` with `rdinit=/init`.
4. The kernel starts `rootfs/init` as PID 1.

The kernel command line sets the UART console (`ttyS0`, 115200).

## What runs in the box

`rootfs/init` does these steps:

1. Mount `/proc`, `/sys`, `/dev`.
2. Load the USB gadget modules.
3. Start a USB gadget with two functions: a serial console (`ttyGS0`, ACM) and a network adapter (ECM). The serial number is `S3V2LINUX`.
4. Set the box address `192.168.77.1` on `usb0`.
5. Start `udhcpd`. It gives only `192.168.77.10`, the host computer.
6. Start BusyBox `httpd` on `192.168.77.1:80`.
7. Start a root shell on `ttyGS0`. It also starts one on `tty1`.

The shell has no password. The USB network has only two hosts. Do not bridge it or route it to another network.

The device tree uses only the 8-bit eMMC (`mmc2`, seen as `mmcblk1`) and the USB OTG port in peripheral mode.
It turns off the SD slot, SDIO Wi-Fi, USB host, Ethernet, SPI, the watchdog and the LEDs.
The board USB-A socket is the OTG port.

### The backup CGI

`rootfs/s3backup.cgi` is served as `/cgi-bin/s3backup`. It only reads (`dd if=...`).

| Request | Answer |
|:--|:--|
| `?info` | JSON with the eMMC name, the sector count, the chunk size and the number of chunks. |
| `?boot0`, `?boot1` | The eMMC hardware boot partitions. |
| `?N` | Chunk `N` of the user area. A chunk is 256 MiB. The last chunk can be shorter. |

Every answer has an `X-SHA256` header with the hash of the bytes that follow. `tools/backup_emmc.py` checks it.

## Differences from the build that I tested on the box

| Change | Why |
|:--|:--|
| The TF-A diagnostic patch is dropped. | It was a debug aid. The loader uses plain TF-A. |
| `udhcpd` gives only `192.168.77.10`. | The host is the only other machine on the USB network. |
| `init` no longer mounts an eMMC partition. | The tested `init` mounted partition 25 of the old layout. The new one never touches the eMMC. |
| `s3backup.cgi` is new. | The old CGI file was lost. I wrote it again. I did not run it on the box. |

The U-Boot patch `0001-s3-ramlinux-trace-and-read-only-ums.patch` stays from the tested build. It does three things:

- It writes a marker to `0x4d000000` at reset and copies the serial output to a buffer there. The DTB keeps 1 MiB at this address free.
- It makes every USB mass storage write a no-op.
- It sets the mass storage LUN to read-only.

The U-Boot defconfig `s3_v2_ramlinux_defconfig` differs from `tanix_tx1_defconfig` in these points:
a custom device tree, `DRAM_CLK=600`, `DX_ODT=0x06010609`, `DX_DRI=0x0d090d0d`, `TPR10=0x00ef3359`, LPDDR3, no LED, `BOOTDELAY=0` and the watchdog off.
`build.sh` sets `BOOTCOMMAND`.

## What I verified

I built the image in Docker on one macOS computer. I compared it with the build that ran on the box:

- The DTB decompiles to the same text as the tested DTB (`dtc -I dtb -O dts`).
- The kernel image is the same.
- The initramfs holds the same nine kernel modules as the tested one. `modules.dep` is correct.

## What I did not verify

- I did not boot the rebuilt `s3-ramlinux-loader.bin` on the box. It builds.
- I did not boot the rebuilt initramfs on the box.
- I did not run `s3backup.cgi` on the box.
- I did not build on an x86 host.
- I did not check that two builds give the same loader, byte for byte.

## Licences

The files `Dockerfile`, `build.sh`, `make_dtb.py` and `rootfs/*` are under the MIT licence of this repository.
`uboot/sun50i-h618-s3-v2-recovery.dts` keeps its own SPDX header (`GPL-2.0+ OR MIT`).
`uboot/0001-s3-ramlinux-trace-and-read-only-ums.patch` follows the licence of U-Boot (GPL-2.0-or-later).
The build downloads U-Boot, TF-A, the Linux kernel and BusyBox. Each keeps its own licence.
