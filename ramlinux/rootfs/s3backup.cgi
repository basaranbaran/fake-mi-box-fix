#!/bin/sh
# Read-only eMMC backup endpoint for tools/backup_emmc.py (BusyBox httpd CGI).
#   ?info           -> disk facts as JSON
#   ?boot0 / ?boot1 -> eMMC hardware boot partitions
#   ?N              -> user area chunk N (256 MiB each, last one may be shorter)
# Every data answer carries X-SHA256 of the exact bytes that follow. This script only reads (dd if=...).
# S3_SYS and S3_DEV (default /sys and /dev) exist only so the script can be tested off the box.
SYS=${S3_SYS:-/sys}
DEVDIR=${S3_DEV:-/dev}
DEV=mmcblk1
CHUNK_MIB=256
CHUNK_BYTES=$((CHUNK_MIB * 1048576))

fail() { printf 'Status: %s\r\nContent-Type: text/plain\r\n\r\n%s\n' "$1" "$2"; exit 0; }

SECTORS=$(cat "$SYS/class/block/$DEV/size" 2>/dev/null)
case "$SECTORS" in ''|*[!0-9]*) fail '503 Service Unavailable' "no $DEV" ;; esac
DISK_BYTES=$((SECTORS * 512))
CHUNKS=$(( (DISK_BYTES + CHUNK_BYTES - 1) / CHUNK_BYTES ))

send() {  # $1 = command that writes the bytes to stdout, $2 = byte count
  sum=$(sh -c "$1" 2>/dev/null | sha256sum | cut -d' ' -f1)
  printf 'Content-Type: application/octet-stream\r\nContent-Length: %s\r\nX-SHA256: %s\r\n\r\n' "$2" "$sum"
  sh -c "$1" 2>/dev/null
}

case "$QUERY_STRING" in
  info)
    printf 'Content-Type: application/json\r\n\r\n'
    printf '{"device":"%s","name":"%s","sectors":%s,"chunk_mib":%s,"chunks":%s,"boot0_bytes":%s,"boot1_bytes":%s}\n' \
      "$DEV" "$(cat "$SYS/class/block/$DEV/device/name")" "$SECTORS" "$CHUNK_MIB" "$CHUNKS" \
      "$(( $(cat "$SYS/class/block/${DEV}boot0/size") * 512 ))" \
      "$(( $(cat "$SYS/class/block/${DEV}boot1/size") * 512 ))"
    ;;
  boot0|boot1)
    bytes=$(( $(cat "$SYS/class/block/${DEV}$QUERY_STRING/size") * 512 ))
    send "dd if='$DEVDIR/${DEV}$QUERY_STRING' bs=1M" "$bytes"
    ;;
  *)
    case "$QUERY_STRING" in ''|*[!0-9]*|0?*) fail '400 Bad Request' 'unknown request' ;; esac
    n=$QUERY_STRING
    [ "$n" -lt "$CHUNKS" ] || fail '404 Not Found' 'chunk out of range'
    offset=$((n * CHUNK_BYTES))
    bytes=$((DISK_BYTES - offset))
    [ "$bytes" -gt "$CHUNK_BYTES" ] && bytes=$CHUNK_BYTES
    send "dd if='$DEVDIR/$DEV' bs=1M skip=$((n * CHUNK_MIB)) count=$CHUNK_MIB" "$bytes"
    ;;
esac
