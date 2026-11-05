#!/bin/bash
# SPDX-License-Identifier: MIT
set -euo pipefail
# Run with a fixed PATH: sudo may preserve the caller's.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
GPU_PCI=''
IFS= read -r GPU_PCI < /etc/thinkpad-nvidia-switch/gpu-pci-address
[[ -n "$GPU_PCI" ]] || { echo 'Installed GPU PCI configuration is empty.' >&2; exit 1; }
exec /usr/local/libexec/thinkpad-nvidia-switch-helper auto-off "$GPU_PCI" --detail
