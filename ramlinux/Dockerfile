# Build environment for the S3_V2.0 RAM Linux (U-Boot + TF-A + Armbian kernel + BusyBox initramfs).
# Usage: see ramlinux/README.md. Nothing in this image talks to a device.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential gcc-aarch64-linux-gnu bison flex bc \
      python3 python3-dev python3-setuptools python3-pyelftools python3-libfdt swig \
      libssl-dev libgnutls28-dev uuid-dev device-tree-compiler \
      busybox-static kmod cpio gzip xz-utils curl ca-certificates git file \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
