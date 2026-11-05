#!/bin/bash
# SPDX-License-Identifier: MIT
# Passwordless restore is available only via this root-owned, narrow sudoers rule.
# Both devices are handled independently to avoid losing CPU restoration when
# the NVIDIA GPU helper fails (or vice versa).
set -uo pipefail
# Run with a fixed PATH: sudo may preserve the caller's.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
GPU_PCI=''
IFS= read -r GPU_PCI < /etc/thinkpad-nvidia-switch/gpu-pci-address
[[ -n "$GPU_PCI" ]] || { echo 'Installed GPU PCI configuration is empty.' >&2; exit 1; }
result=0
/usr/local/libexec/thinkpad-nvidia-switch-helper restore-state "$GPU_PCI" --detail || result=1
/usr/local/libexec/thinkpad-nvidia-switch-cpu-helper restore-saved || result=1
exit "$result"
