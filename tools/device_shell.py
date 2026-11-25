#!/usr/bin/env python3
"""Run shell commands on the box's RAM Linux root console (USB serial, ttyGS0 on the box).

    device_shell.py [--port PORT] [TIMEOUT] < commands

Reads the commands from stdin, types them into the console, prints their output and exits with
the exit status of the last command. TIMEOUT is in seconds (default 30). Without --port the
console is found by its USB serial number S3V2LINUX:
    macOS  /dev/cu.usbmodemS3V2LINUX*
    Linux  /dev/serial/by-id/*S3V2LINUX*, else the only /dev/ttyACM*

Use one line per command where the exit status matters: only the status of the last line is
returned. install.py imports run() from this file.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import select
import sys
import termios
import time
import uuid

MAX_LINE = 3500  # the tty line buffer on the box is 4096 bytes


class ConsoleError(RuntimeError):
    """The console could not be found, opened or did not answer in time."""


def find_port() -> str:
    """Return the console device path, or raise ConsoleError."""
    for pattern in ('/dev/cu.usbmodemS3V2LINUX*', '/dev/serial/by-id/*S3V2LINUX*'):
        found = sorted(glob.glob(pattern))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ConsoleError(f'more than one console matches {pattern}: {found}; use --port')
    acm = sorted(glob.glob('/dev/ttyACM*'))
    if len(acm) == 1:
        return acm[0]
    raise ConsoleError('S3 console not found (is the RAM Linux running and the USB cable in the OTG port?)'
                       if not acm else f'several /dev/ttyACM* devices {acm}; use --port')


def run(command: str, timeout: float = 30, port: str | None = None) -> tuple[int, str]:
    """Type `command` (one or more lines) into the console. Returns (exit status, output text)."""
    lines = command.strip('\n').split('\n')
    if any(len(line.encode()) > MAX_LINE for line in lines):
        raise ConsoleError(f'a command line is longer than {MAX_LINE} bytes')
    path = port or find_port()
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        raise ConsoleError(f'cannot open {path}: {exc}') from None
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0  # raw: no input/output processing, no echo
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, b'\r')
        time.sleep(0.3)
        while select.select([fd], [], [], 0.1)[0]:  # drop the old prompt and any leftovers
            os.read(fd, 65536)
        marker = 'S3_DONE_' + uuid.uuid4().hex
        payload = ('stty -echo\r' + '\r'.join(lines) +
                   '\rprintf "\\n' + marker + ':%s\\n" "$?"\r').encode()
        for start in range(0, len(payload), 512):
            os.write(fd, payload[start:start + 512])
            time.sleep(0.02)
        done = re.compile(re.escape(marker.encode()) + rb':(\d+)\r?\n')
        data = b''
        deadline = time.monotonic() + timeout
        while not (match := done.search(data)):
            left = deadline - time.monotonic()
            if left <= 0:
                raise ConsoleError(f'command did not finish within {timeout:.0f} s')
            if select.select([fd], [], [], min(left, 0.3))[0]:
                data += os.read(fd, 65536)
        return int(match.group(1)), data[:match.start()].decode(errors='replace').rstrip()
    finally:
        os.close(fd)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--port', help='console device (default: auto-detect)')
    p.add_argument('timeout', nargs='?', type=float, default=30.0, metavar='TIMEOUT')
    a = p.parse_args(argv)
    try:
        status, output = run(sys.stdin.read(), a.timeout, a.port)
    except ConsoleError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 124
    print(output)
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
