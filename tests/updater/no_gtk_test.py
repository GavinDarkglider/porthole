#!/usr/bin/env python3
"""
    The engine runs without GTK: porthole[-gtk] (servers, cluster nodes,
    RAM-only images) installs only these parts. Every engine module must
    import, and the update service must answer, with gi not installed;
    this blocks gi and checks.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


class BlockGi:
    def find_spec(self, name, path=None, target=None):
        if name == "gi" or name.startswith("gi."):
            raise ImportError("gi blocked: the engine must not need GTK")


sys.meta_path.insert(0, BlockGi())

ENGINE = ["porthole.updater." + m for m in (
    "analyze", "autopilot", "commands", "ondemand", "repopool", "resolve", "resume",
    "runner", "service", "system", "targets", "viewer", "window", "session", "news")]
ENGINE += ["porthole.plugins"]
# each plugin's root half and whatever it imports
for name in sorted(os.listdir(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "..", "..", "porthole", "plugins"))):
    if os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                   "porthole", "plugins", name, "root.py")):
        ENGINE.append("porthole.plugins.%s.root" % name)
ENGINE += ["porthole.plugins.crossdev." + m for m in (
    "roots", "build", "emulate", "conf", "toolchain", "checks", "packages", "firstboot")]

failed = 0
for module in ENGINE:
    try:
        importlib.import_module(module)
        print("ok   %s" % module)
    except Exception as e:
        failed += 1
        print("FAIL %s: %r" % (module, e))

# the service's argument handling, without running anything
from porthole.updater import service
sys.argv = ["porthole-update-service", "--help"]
try:
    service.main()
except SystemExit as e:
    if e.code not in (0, None):
        failed += 1
        print("FAIL service --help exited %r" % e.code)
    else:
        print("ok   porthole-update-service --help")

loaded = [m for m in sys.modules if m == "gi" or m.startswith("gi.")]
if loaded:
    failed += 1
    print("FAIL gi was loaded: %s" % loaded)
print("%d failed" % failed)
sys.exit(1 if failed else 0)
