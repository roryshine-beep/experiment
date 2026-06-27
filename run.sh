#!/usr/bin/env bash
#
# Launch Dominion (terminal edition) under a hard memory/CPU ceiling.
#
# The TUI uses only a few MB, so this cap is just insurance: if anything ever
# runs away, the kernel OOM-kills this process group instead of the machine.
#
# Tunables (override via env): MEM_MAX, CPU_QUOTA
set -euo pipefail

MEM_MAX="${MEM_MAX:-256M}"
CPU_QUOTA="${CPU_QUOTA:-100%}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec systemd-run --user --scope --collect \
  --unit="dominion-$$" \
  -p MemoryMax="${MEM_MAX}" \
  -p MemorySwapMax=0 \
  -p MemoryHigh="${MEM_MAX}" \
  -p CPUQuota="${CPU_QUOTA}" \
  -p TasksMax=50 \
  python3 "${DIR}/dominion.py"
