# Tools

Host-side scripts for the guide ([REHBER.md](../REHBER.md), Turkish). Run every command from the repository root.

Rules for all tools:

- No tool writes to the eFuse.
- Only `install.py` writes to the eMMC. It writes only when you add `--write`. Without `--write` it is a dry run.
- `boot_ramlinux.py` writes only to the RAM of the box.
- I did not run these rewritten tools on a device. See the table in the guide.

Names used below: `LEMON.img` is the Lemon firmware file. `WORK` is the folder of unpacked files. `BACKUP` is the folder of the eMMC backup.

## Overview

| Tool | Needs the box | What it does |
|:--|:--|:--|
| `imagewty.py` | no | Lists and unpacks an Allwinner IMAGEWTY firmware image. |
| `toc0.py` | no | Checks a TOC0 file: checksum, firmware hash, RSA signature. |
| `patch_toc0_dram.py` | no | Writes the S3_V2.0 DRAM values into a TOC0 file. |
| `prepare_gpt.py` | no | Makes the GPT for the 14.7 GiB eMMC from the firmware GPT and the backup. |
| `check_rotpk.py` | FEL | Compares the key hash in the SoC with the key hash in a TOC0 file. |
| `boot_ramlinux.py` | FEL | Starts the RAM Linux in the RAM of the box. |
| `device_shell.py` | USB serial console | Runs shell commands from stdin on the box. |
| `backup_emmc.py` | USB network | Copies the eMMC to the host. Read only. |
| `install.py` | USB serial console and USB network | Writes the boot stage and the Android stage. |
| `sunxi-fel-64KiB-reads.patch` | - | Patch for `sunxi-fel`. See below. |

## sunxi-fel

Build `sunxi-fel` from [linux-sunxi/sunxi-tools](https://github.com/linux-sunxi/sunxi-tools) with the patch `sunxi-fel-64KiB-reads.patch`.
The patch splits each read into 64 KiB requests. A big single read locked the USB connection.

macOS: `brew install libusb pkgconf dtc`.
Debian and Ubuntu: `sudo apt install build-essential git libusb-1.0-0-dev zlib1g-dev pkg-config libfdt-dev device-tree-compiler`.

```sh
git clone https://github.com/linux-sunxi/sunxi-tools
cd sunxi-tools
git checkout a08a24a2aa5804bde7fa6ea8cdb08135d018da33
git apply ../tools/sunxi-fel-64KiB-reads.patch
make sunxi-fel
cp sunxi-fel ..
```

The commit `a08a24a2...` is the version that I tested the patch with.

## imagewty.py

```sh
python3 tools/imagewty.py android LEMON.img WORK
python3 tools/imagewty.py list LEMON.img
python3 tools/imagewty.py extract LEMON.img OUTDIR NAME [NAME ...]
```

`android` writes the files that the installer needs into `WORK`:
`toc0.fex`, `toc1.fex`, `sunxi_gpt.fex`, the ten Android partition files and `manifest.json` (size and SHA-256 of each file).
It expands the sparse `super.fex` into `super.raw`.
It works offline. It does not support encrypted images.

## toc0.py

```sh
python3 tools/toc0.py FILE [FILE ...]
```

Prints JSON for each file. It never touches a device. `check_rotpk.py` and `patch_toc0_dram.py` use it.

## patch_toc0_dram.py

```sh
python3 tools/patch_toc0_dram.py WORK/toc0.fex WORK/toc0-s3.fex
```

Changes four DRAM fields and the TOC0 checksum. The certificate and the signed firmware stay the same.
The tool checks that no other byte changed and that the checksum, the firmware hash and the RSA signature are still valid.
The options `--dram-clk`, `--dx-odt`, `--dx-dri` and `--tpr10` set other values. Run `--help` for all options.
The defaults are the S3_V2.0 values: `DRAM_CLK=600`, `DX_ODT=0x06010609`, `DX_DRI=0x0d090d0d`, `TPR10=0x00ef3359`.
Another board needs its own values. Do not use the defaults on another board.

Expected SHA-256 of the files:

| File | SHA-256 |
|:--|:--|
| `toc0.fex` (input) | `70d3b039c0e82dc06cd310cfb4abf93d5263f2df5fa47fb5b3084496e8f3032c` |
| `toc1.fex` | `a81a0b2975322aa746ab4a0e7218fe91a6f7441d0b4ec6afe26204c88d81d18d` |
| `toc0-s3.fex` (output) | `64283fc194ecb909b7168dadb6ff797741016ceee4a2074e49ccd2e84629b94c` |

## check_rotpk.py

```sh
python3 tools/check_rotpk.py --fel ./sunxi-fel WORK/toc0.fex
```

Reads the root key hash area of the SoC through FEL. Compares it with the root key in `WORK/toc0.fex`.
Prints `MATCH` or `NO MATCH` for each file. It does not print or save the value from the SoC.
Exit status: 0 = match, 1 = no match, 2 = usage or device error. On `NO MATCH`, stop.

```sh
python3 tools/check_rotpk.py --offline WORK/toc0.fex
```

`--offline` needs no box. It prints the checksum and signature checks of the file and its root key hashes. It reads no device value.

## boot_ramlinux.py

```sh
python3 tools/boot_ramlinux.py --fel ./sunxi-fel --dir out
python3 tools/boot_ramlinux.py --fel ./sunxi-fel --dir out --dry-run
```

`--dir` is the output folder of the RAM Linux build (see [ramlinux/README.md](../ramlinux/README.md)).
The tool checks `SHA256SUMS` in that folder first.
`--dry-run` checks the files and prints the `sunxi-fel` command. It does not run it.
After about 20 seconds the box shows the USB serial console and the USB network.

## device_shell.py

```sh
python3 tools/device_shell.py [--port PORT] [TIMEOUT] < commands
```

Reads shell commands from stdin. Runs them on the USB serial console of the RAM Linux. Prints the output.
Without `--port`, it uses the serial port of the box: `/dev/cu.usbmodemS3V2LINUX*` on macOS, `/dev/ttyACM*` on Linux.
`TIMEOUT` is the wait for the commands to finish, in seconds (default 30).
The exit status is the status of the last command. Use one line per command when the status matters.

```sh
printf 'uname -a\ncat /proc/partitions\n' | python3 tools/device_shell.py 15
```

The shell is a root shell. A command that you send can write to the eMMC.

## backup_emmc.py

```sh
python3 tools/backup_emmc.py BACKUP
```

Copies boot0, boot1 and the whole user area of the eMMC to `BACKUP`. It reads from the box over HTTP.

- The host needs the address `192.168.77.10` on the USB network. The box is `192.168.77.1`.
- The tool asks the CGI `/cgi-bin/s3backup` for `?info`, `?boot0`, `?boot1` and `?N` (256 MiB chunks).
- It keeps a file only when the length and the `X-SHA256` header match.
- `BACKUP` gets `info.json`, `boot0.bin`, `boot1.bin`, `chunk-00.bin` and so on, and `manifest.json`.
- Run the same command again to resume. The tool skips files that are already verified.

## prepare_gpt.py

```sh
python3 tools/prepare_gpt.py --donor WORK/sunxi_gpt.fex --backup BACKUP --out WORK
```

Takes the GPT of the firmware (`--donor`) and the disk size from the old GPT in `BACKUP`.
Reads the disk size and the old GPT from the backup (`info.json` and `chunk-00.bin`).
Renames `UDISK` to `userdata` and extends it to the last usable sector.
Keeps the disk GUID of the old GPT. The partition GUIDs are deterministic (`uuid5`).
Writes `gpt-primary.bin`, `gpt-backup.bin` and `layout.json` into `WORK`.

## install.py

```sh
python3 tools/install.py boot    --backup BACKUP --files WORK [--write]
# reboot check: a logo appears, the box does not return to FEL
python3 tools/install.py android --backup BACKUP --files WORK [--write]
```

Without `--write`, both stages are a dry run. They print what they would do.

| Stage | What it writes |
|:--|:--|
| `boot` | `toc0-s3.fex` to the start of boot0, and to LBA 16 and LBA 256 of the user area. `toc1.fex` to LBA 32800. It reads every write back and checks the SHA-256. |
| `android` | The ten Android partition files and the GPT. It keeps `frp`, `private`, `media_data` and `Reserve0`. It zeroes `metadata` and the first part of `customize` and `userdata`. It writes the backup GPT first and the primary GPT last. |

Checks before a write:

- The eMMC name and size must be the expected ones.
- The GPT on the box must be the GPT in `BACKUP`.
- The TOC ranges must not overlap the old GPT and the old partitions.
- The `android` stage refuses to run when the target GPT is already on the box.

`install.py` starts a small HTTP server on the host. The box downloads the files from it.
The host firewall must allow this. On macOS, allow incoming connections for Python.
