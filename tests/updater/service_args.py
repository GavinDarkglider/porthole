#!/usr/bin/env python3
"""
    Check that porthole-update-service parses every form of `submit` the
    update assistant, the tray and Porthole send (no root needed; nothing
    is submitted). A positional that swallowed the rest of the command
    line once broke `submit update --plan`.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import porthole.updater.service as service  # noqa: E402

CASES = [
    (["submit", "update", "--plan", "2026-10-29T16:17:22-0700"],
     {"kind": "update", "plan": "2026-10-29T16:17:22-0700", "cmdline": []}),
    (["submit", "check", "--no-sync", "--choices", '{"exclude": []}'],
     {"kind": "check", "no_sync": True, "choices": '{"exclude": []}', "cmdline": []}),
    (["submit", "check"], {"kind": "check", "no_sync": False, "sync_now": False, "cmdline": []}),
    (["submit", "check", "--sync-now"], {"kind": "check", "sync_now": True}),
    (["submit", "emerge", "--", "emerge", "--oneshot", "=sys-libs/zlib-1.3.2-r1"],
     {"kind": "emerge", "plan": None,
      "cmdline": ["emerge", "--oneshot", "=sys-libs/zlib-1.3.2-r1"]}),
    (["submit", "emerge", "--", "USE=gtk -qt", "emerge", "--update", "@world"],
     {"kind": "emerge", "cmdline": ["USE=gtk -qt", "emerge", "--update", "@world"]}),
    (["submit", "install", "--request", '{"targets": ["app-misc/foo"]}'],
     {"kind": "install", "request": '{"targets": ["app-misc/foo"]}', "plan": None}),
    (["submit", "install", "--plan", "/var/lib/porthole/installs/x.json",
      "--generated", "2026-10-29T17:00:00-0700"],
     {"kind": "install", "plan": "/var/lib/porthole/installs/x.json",
      "generated": "2026-10-29T17:00:00-0700"}),
    (["cancel", "20261030-090718-install-a3f2", "--drop"],
     {"job": "20261030-090718-install-a3f2", "drop": True}),
    (["cancel"], {"job": None, "drop": False}),
    (["keep-building"], {"job": None}),
    (["licences", "redistributable"], {"policy": "redistributable", "target": None}),
    (["repo", "add", "guru", "--sync-type", "git", "--uri", "https://github.com/gentoo-mirror/guru.git"],
     {"action": "add", "name": "guru", "sync_type": "git", "no_auto_sync": False}),
    (["repo", "remove", "guru", "--delete-files"], {"action": "remove", "delete_files": True}),
    (["repo", "sync", "pentoo"], {"action": "sync", "name": "pentoo"}),
    (["repo", "add", "switch", "--sync-type", "git", "--uri",
      "https://gitlab.com/bell07/gentoo-switch_overlay.git", "--systems"],
     {"action": "add", "name": "switch", "systems": True, "target": None, "folder": None}),
    (["repo", "add", "switch", "--folder", "/projects/switch_overlay", "--target", "switch"],
     {"action": "add", "folder": "/projects/switch_overlay", "target": "switch",
      "systems": False, "uri": None}),
    (["repo", "add", "mine", "--folder", "/usr/local/portage"],
     {"action": "add", "folder": "/usr/local/portage", "target": None}),
    (["repo", "use", "pentoo", "--target", "switch"],
     {"action": "use", "name": "pentoo", "target": "switch"}),
    (["repo", "drop", "pentoo", "--target", "switch"], {"action": "drop", "target": "switch"}),
    (["repo", "move", "Nintendo_Switch", "--systems"],
     {"action": "move", "name": "Nintendo_Switch", "systems": True}),
    (["repo", "remove", "switch", "--systems", "--delete-files"],
     {"action": "remove", "systems": True, "delete_files": True}),
    (["plugin", "crossdev", "new-root", "switch", "--profile", "switch:nintendo_switch/23.0",
      "--title", "My Switch", "--repo", "pentoo"],
     {"name": "crossdev", "args": ["new-root", "switch", "--profile",
                                   "switch:nintendo_switch/23.0", "--title", "My Switch",
                                   "--repo", "pentoo"]}),
    (["plugin", "crossdev", "toolchain", "switch"],
     {"name": "crossdev", "args": ["toolchain", "switch"]}),
    (["binhost", "add", "desktop", "--uri", "http://desktop/binpkgs", "--no-verify"],
     {"action": "add", "name": "desktop", "no_verify": True}),
    (["binhost", "remove", "desktop"], {"action": "remove", "name": "desktop"}),
    (["binpkgs", "upload"], {"action": "upload"}),
    (["plugin", "crossdev", "check", "arm64"], {"name": "crossdev", "args": ["check", "arm64"]}),
    (["plugin", "crossdev", "--", "check"], {"name": "crossdev", "args": ["check"]}),
    (["profile", "default/linux/amd64/23.0/desktop"], {"name": "default/linux/amd64/23.0/desktop",
                                                       "target": None}),
    (["global-use", '{"use": {"opus": false}}'], {"changes": '{"use": {"opus": false}}',
                                                  "target": None}),
    (["licences", "system", "--target", "arm-board"], {"policy": "system", "target": "arm-board"}),
    (["keep-building", "20261030-162651-install-f9d4"], {"job": "20261030-162651-install-f9d4"}),
    (["submit", "update", "--plan", "2026-10-29T16:17:22-0700", "--later"],
     {"kind": "update", "plan": "2026-10-29T16:17:22-0700", "later": True}),
    (["submit", "install", "--request", '{"targets": ["@world"]}', "--expect",
      "0123456789abcdef01234567", "--later"],
     {"kind": "install", "expect": "0123456789abcdef01234567", "later": True}),
    (["submit", "command", "--", "eselect kernel list"],
     {"kind": "command", "cmdline": ["eselect kernel list"], "later": False}),
    (["plan", "--request", '{"targets": ["@world"]}'], {"request": '{"targets": ["@world"]}'}),
    (["sets", "preserved-rebuild"], {"names": ["preserved-rebuild"]}),
    (["session", "--minutes", "60"], {"minutes": 60}),
    (["config", "build_window=01:00-06:00", "idle_minutes=20"],
     {"settings": ["build_window=01:00-06:00", "idle_minutes=20"]}),
    (["news", "read", "--repo", "gentoo", "2026-05-03-binpkg-changes"],
     {"state": "read", "repo": "gentoo", "names": ["2026-05-03-binpkg-changes"]}),
]


def parsed(argv):
    """The namespace main() would act on"""
    seen = []
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        seen.append(original(self, args, namespace))
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = capture
    try:
        service.main(argv)
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = original
    return seen[0]


def main():
    failed = 0
    for argv, expected in CASES:
        args = parsed(argv)
        wrong = {k: getattr(args, k) for k, v in expected.items() if getattr(args, k) != v}
        print("%s %s" % ("ok  " if not wrong else "FAIL", " ".join(argv)))
        if wrong:
            print("     got", wrong)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
