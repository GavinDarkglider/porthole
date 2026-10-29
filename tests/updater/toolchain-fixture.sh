#!/bin/bash
# Exercise toolchain staging: a new GCC major (17 snapshot) plus a newer
# glibc, accepted in a scratch copy of /etc/portage. Read-only; needs root.
#
# Expected: toolchain stage first, then an approval stage switching GCC.
set -eu
src=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d /var/tmp/porthole-fixture.XXXXXX)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/config/etc"
cp -a /etc/portage "$work/config/etc/portage"
ln -sfn "$(readlink -f /etc/portage/make.profile)" "$work/config/etc/portage/make.profile"
printf '<sys-devel/gcc-17.0.9999 **\nsys-libs/glibc ~amd64\n' \
    > "$work/config/etc/portage/package.accept_keywords/zz-fixture"

"$src/scripts/porthole-update-check" --local --config-root "$work/config" \
    --json "$work/plan.json" -- --oneshot sys-devel/gcc:17 sys-libs/glibc
python3 - "$work/plan.json" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
titles = [s["title"] for s in plan["stages"]]
print("status:", plan["status"])
assert titles[0].startswith("Update the toolchain first"), titles
assert any(s.get("approval") and "gcc-config" in s["commands"][0] for s in plan["stages"]), titles
assert plan["full_rebuild"] and plan["toolchain"]["libc"]
print("ok")
PY
