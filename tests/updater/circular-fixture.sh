#!/bin/bash
# Exercise cycle breaking: resolve libwebp[tiff] + tiff[webp] into an empty
# ROOT/SYSROOT, which also hits the usual bootstrap cycles (python/util-linux,
# pam/elogind, ...). Read-only; needs root. Takes a few minutes.
#
# Expected: status "needs-approval" with a verified two-stage plan.
set -eu
src=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d /var/tmp/porthole-fixture.XXXXXX)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/config/etc" "$work/root"
cp -a /etc/portage "$work/config/etc/portage"
ln -sfn "$(readlink -f /etc/portage/make.profile)" "$work/config/etc/portage/make.profile"
printf 'media-libs/libwebp tiff\nmedia-libs/tiff webp\n' \
    > "$work/config/etc/portage/package.use/zz-fixture"

"$src/scripts/porthole-update-check" --local --config-root "$work/config" \
    --json "$work/plan.json" -- \
    --oneshot --root="$work/root" --sysroot="$work/root" \
    media-libs/libwebp media-libs/tiff | sed -n '1,/^Updates:/p'
python3 -c "import json,sys; p=json.load(open(sys.argv[1])); print('status:', p['status']); sys.exit(p['status'] != 'needs-approval')" "$work/plan.json"
