#!/bin/bash
# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
# Report whether the container's Intel GPU userspace stack has updates
# available in the configured repos. Read-only: never installs anything.
set -e
echo "== installed =="
dpkg -l | awk '/^ii/ && /libze|intel-metrics|intel-opencl|libigc/ {printf "%-40s %s\n", $2, $3}'
echo
echo "== checking repos =="
apt-get update -qq 2>/dev/null
UPG=$(apt list --upgradable 2>/dev/null | grep -Ei 'libze|intel-metrics|intel-opencl|libigc' || true)
if [ -n "$UPG" ]; then
    echo "UPDATES AVAILABLE:"
    echo "$UPG"
    exit 1
else
    echo "up to date"
fi
