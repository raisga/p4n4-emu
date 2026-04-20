#!/usr/bin/env bash
# Install QEMU binfmt_misc handlers for ARM64 on the host kernel.
# Requires Docker and a kernel with binfmt_misc support.
set -euo pipefail

echo "Installing QEMU binfmt_misc for ARM64..."
docker run --privileged --rm tonistiigi/binfmt --install arm64

echo "Verifying registration..."
if [ -f /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
    echo "OK: /proc/sys/fs/binfmt_misc/qemu-aarch64 registered."
else
    echo "WARNING: Registration may not have persisted (check binfmt_misc mount)."
fi
