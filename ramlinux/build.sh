#!/usr/bin/env bash
# Build the S3_V2.0 RAM Linux inside the container from ramlinux/Dockerfile.
#   docker build -t s3-ramlinux-build ramlinux
#   docker run --rm -v "$PWD/ramlinux:/src:ro" -v "$PWD/out:/out" s3-ramlinux-build bash /src/build.sh
# Output in /out: s3-ramlinux-loader.bin, Image.gz, initramfs.cpio.gz, s3-v2-linux.dtb, SHA256SUMS.
# Nothing here talks to a device.
set -euo pipefail

UBOOT_TAG=v2026.10
TFA_COMMIT=9799b799fb51edef36607bcd534b5510aa4156bc
# Armbian kernel that was used and tested on the box (6.18.44). Set ARMBIAN_KERNEL=latest to
# take the newest linux-image-current-sunxi64 from the Armbian index instead (not tested).
ARMBIAN_KERNEL=${ARMBIAN_KERNEL:-pinned}
ARMBIAN=https://apt.armbian.com
PIN_IMAGE=pool/main/l/linux-6.18.44/linux-image-current-sunxi64_26.8.3_arm64__6.18.44-S1efe-D5397-P5708-C4e0c-H2153-HK01ba-Vc222-B4990-R448a.deb
PIN_IMAGE_SHA=d0c9253a77d1fbe94790cd1259ef0982c85aa24cfac0b334b8e63f9df5426b25
PIN_DTB=pool/main/l/linux-dtb-current-sunxi64/linux-dtb-current-sunxi64_26.8.3_arm64__6.18.44-S1efe-D5397-P5708-C4e0c-H2153-HK01ba-Vc222-B4990-R448a.deb
PIN_DTB_SHA=7aa765c8ddd4ea72938c3cbd40349079dad7d7c75d0dfdb7cd5c1030c51dec74

SRC=/src
OUT=/out
W=/work
CC=aarch64-linux-gnu-
mkdir -p "$W" "$OUT"
cd "$W"

fetch() {  # url sha256 dest
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --quiet
}

echo "== 1/6 Armbian kernel and DTB packages"
if [ "$ARMBIAN_KERNEL" = latest ]; then
  curl -fsSL "$ARMBIAN/dists/noble/main/binary-arm64/Packages.gz" | gzip -dc > Packages
  pick() { awk -v p="$1" '$1=="Package:"{n=$2} n==p&&$1=="Filename:"{f=$2} n==p&&$1=="SHA256:"{print f, $2}' Packages | tail -n 1; }
  read -r PIN_IMAGE PIN_IMAGE_SHA < <(pick linux-image-current-sunxi64)
  read -r PIN_DTB PIN_DTB_SHA < <(pick linux-dtb-current-sunxi64)
fi
fetch "$ARMBIAN/$PIN_IMAGE" "$PIN_IMAGE_SHA" image.deb
fetch "$ARMBIAN/$PIN_DTB" "$PIN_DTB_SHA" dtb.deb
rm -rf pkg && mkdir pkg
dpkg-deb -x image.deb pkg
dpkg-deb -x dtb.deb pkg
KVER=$(ls pkg/lib/modules)
echo "kernel $KVER"

echo "== 2/6 Kernel image and DTB"
gzip -9 -n -c "pkg/boot/vmlinuz-$KVER" > "$OUT/Image.gz"
python3 "$SRC/make_dtb.py" "pkg/boot/dtb-$KVER/allwinner/sun50i-h618-orangepi-zero3.dtb" "$OUT/s3-v2-linux.dtb"

echo "== 3/6 BusyBox initramfs"
R=$W/rootfs
rm -rf "$R"
mkdir -p "$R"/{bin,sbin,proc,sys,dev,run,tmp,etc,www/cgi-bin,lib/modules/$KVER}
cp /bin/busybox "$R/bin/busybox"
for app in $(/bin/busybox --list); do [ -e "$R/bin/$app" ] || ln -s busybox "$R/bin/$app"; done
ln -sf ../bin/busybox "$R/sbin/modprobe"
for mod in sunxi libcomposite usb_f_acm usb_f_ecm; do
  modprobe -d pkg -S "$KVER" --show-depends "$mod" | awk '$1=="insmod"{print $2}'
done | sort -u | while read -r ko; do
  rel=${ko#*pkg/}
  mkdir -p "$R/$(dirname "$rel")"
  cp "$ko" "$R/$rel"
done
for f in modules.builtin modules.builtin.modinfo modules.order; do
  [ -e "pkg/lib/modules/$KVER/$f" ] && cp "pkg/lib/modules/$KVER/$f" "$R/lib/modules/$KVER/"
done
depmod -b "$R" "$KVER"
install -m 755 "$SRC/rootfs/init" "$R/init"
install -m 755 "$SRC/rootfs/console" "$R/bin/console"
install -m 644 "$SRC/rootfs/udhcpd.conf" "$R/etc/udhcpd.conf"
install -m 755 "$SRC/rootfs/s3backup.cgi" "$R/www/cgi-bin/s3backup"
(cd "$R" && find . -print0 | sort -z | cpio --null -o -H newc --quiet | gzip -9 -n) > "$OUT/initramfs.cpio.gz"

echo "== 4/6 TF-A BL31 ($TFA_COMMIT)"
rm -rf tfa && git init -q tfa
git -C tfa fetch -q --depth 1 https://github.com/ARM-software/arm-trusted-firmware.git "$TFA_COMMIT"
git -C tfa checkout -q FETCH_HEAD
make -s -C tfa CROSS_COMPILE=$CC PLAT=sun50i_h616 DEBUG=0 bl31 >/dev/null
BL31=$W/tfa/build/sun50i_h616/release/bl31.bin

echo "== 5/6 U-Boot $UBOOT_TAG with S3 DRAM settings"
rm -rf uboot
git clone -q --depth 1 --branch "$UBOOT_TAG" https://github.com/u-boot/u-boot.git uboot
git -C uboot apply "$SRC/uboot/0001-s3-ramlinux-trace-and-read-only-ums.patch"
cp "$SRC/uboot/sun50i-h618-s3-v2-recovery.dts" uboot/dts/upstream/src/arm64/allwinner/
cp "$SRC/uboot/s3_v2_ramlinux_defconfig" uboot/configs/
INITRD_HEX=$(printf '%x' "$(stat -c %s "$OUT/initramfs.cpio.gz")")
BOOTCMD="setenv bootargs console=ttyS0,115200 earlycon=uart8250,mmio32,0x05000000 rdinit=/init nokaslr panic=0 loglevel=7 clk_ignore_unused regulator_ignore_unused; setenv kernel_comp_addr_r 0x50000000; setenv kernel_comp_size 0x4000000; booti 0x42000000 0x48000000:$INITRD_HEX 0x4c000000"
make -s -C uboot CROSS_COMPILE=$CC s3_v2_ramlinux_defconfig
uboot/scripts/config --file uboot/.config --set-str BOOTCOMMAND "$BOOTCMD"
make -s -C uboot CROSS_COMPILE=$CC olddefconfig
make -s -C uboot CROSS_COMPILE=$CC BL31="$BL31" SCP=/dev/null -j"$(nproc)" >/dev/null
cp uboot/u-boot-sunxi-with-spl.bin "$OUT/s3-ramlinux-loader.bin"
echo "$BOOTCMD" > "$OUT/bootcommand.txt"

echo "== 6/6 Checksums"
cd "$OUT"
sha256sum s3-ramlinux-loader.bin Image.gz initramfs.cpio.gz s3-v2-linux.dtb bootcommand.txt > SHA256SUMS
echo "kernel=$KVER uboot=$UBOOT_TAG tfa=$TFA_COMMIT" > BUILDINFO
cat SHA256SUMS BUILDINFO
