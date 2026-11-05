#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Detect exactly one NVIDIA PCI display controller via sysfs.

Usage: detect_nvidia_gpu.py [SYSFS_PCI_DEVICES_DIR]
The optional directory argument is useful for tests; normal installs use /sys.
"""
from pathlib import Path
import re
import sys

ADDRESS_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def detect(root: Path) -> list[str]:
    found = []
    if not root.is_dir():
        raise RuntimeError(f"PCI sysfs directory not found: {root}")
    for device in sorted(root.iterdir()):
        if not ADDRESS_RE.fullmatch(device.name):
            continue
        try:
            vendor = (device / "vendor").read_text().strip().lower()
            cls = (device / "class").read_text().strip().lower()
        except OSError:
            continue
        # PCI class 0x03xxxx means display controller (VGA/3D/display).
        if vendor == "0x10de" and re.fullmatch(r"0x03[0-9a-f]{4}", cls):
            found.append(device.name.lower())
    return found


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/sys/bus/pci/devices")
    if len(sys.argv) > 2:
        print(f"Usage: {Path(sys.argv[0]).name} [SYSFS_PCI_DEVICES_DIR]", file=sys.stderr)
        return 2
    try:
        devices = detect(root)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if len(devices) == 0:
        print("No NVIDIA PCI display controller was found. No files were installed.", file=sys.stderr)
        return 1
    if len(devices) > 1:
        print("Multiple NVIDIA PCI display controllers were found; refusing to guess:", file=sys.stderr)
        for address in devices:
            print(f"  {address}", file=sys.stderr)
        return 1
    print(devices[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
