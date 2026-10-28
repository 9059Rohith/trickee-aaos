#!/usr/bin/env bash
set -euo pipefail
: "${AOSP_DEST:?Set AOSP_DEST to a directory with sufficient space on a Linux build host}"
mkdir -p "$AOSP_DEST"
cd "$AOSP_DEST"
repo init -u https://android.googlesource.com/platform/manifest -b android-17.0.0_r1 --partial-clone
repo sync -c -j8
printf 'Pinned manifest: '; git -C .repo/manifests rev-parse HEAD
printf 'Select a car target from lunch and build on the supported Linux host.\n'
