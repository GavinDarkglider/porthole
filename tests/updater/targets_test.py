#!/usr/bin/env python3
"""
    Targets: targets.conf parsing, and a plan for another system writing
    its fixes and temporary USE into that system's etc/portage only.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from porthole.updater import runner, targets  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    failed += not ok
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, ("  (%s)" % detail) if detail else ""))


work = tempfile.mkdtemp(prefix="porthole-targets-")
board = os.path.join(work, "usr", "aarch64-unknown-linux-gnu")
os.makedirs(os.path.join(board, "etc", "portage", "package.use"))
conf = os.path.join(work, "targets.conf")
with open(conf, "w") as f:
    f.write("[arm-board]\ntitle = Kitchen Pi\nconfig_root = %s\nroot = %s\n\n"
            "[local]\nroot = /tmp/evil\n\n[Bad Name]\nroot = /x\n\n"
            "[relative]\nroot = usr/x\n\n[slash]\nroot = /\n" % (board, board))
found = targets.load(conf)
check("this computer always there, never redefined", found["local"] is targets.THIS_COMPUTER)
check("only valid targets load", sorted(found) == ["arm-board", "local"], sorted(found))
pi = found["arm-board"]
check("emerge options", pi.emerge_args() == ["--config-root=" + board, "--root=" + board,
                                              "--sysroot=" + board], pi.emerge_args())
check("this computer: no options", targets.THIS_COMPUTER.emerge_args() == [])
check("state dir", pi.state_dir("/var/lib/porthole") == "/var/lib/porthole/targets/arm-board"
      and targets.THIS_COMPUTER.state_dir("/var/lib/porthole") == "/var/lib/porthole")

# a plan for the board: the runner's steps aim at its etc/portage
targets.load = lambda path=None: found
plan = {"target": "arm-board", "generated": "2026-10-30T12:00:00-0700",
        "fixes": [{"kind": "use", "cpv": "lib/x-1", "line": ">=lib/x-1 foo",
                   "explanation": ["needed"]}],
        "stages": [{"title": "Update", "commands": [["emerge", "-1", "lib/x"]],
                    "config": {"use": ["lib/y -bar"]}}]}
steps = runner.plan_steps(plan)
check("apply-config names the board", steps[0]["target"] == "arm-board"
      and board in steps[0]["title"], steps[0]["title"])
check("temporary USE goes to the board", os.path.join(board, "etc", "portage", "package.use",
                                                      "zz-porthole-temp") in steps[1]["shell"])
written = runner.write_fixes(plan["fixes"], targets.of_plan(plan).portage_dir)
check("fix written into the board's etc/portage",
      written and written[0][0] == os.path.join(board, "etc", "portage", "package.use",
                                                "zz-porthole"), written)
check("not into this computer's", not os.path.exists("/etc/portage/package.use/zz-porthole")
      or ">=lib/x-1 foo" not in open("/etc/portage/package.use/zz-porthole").read())
runner.last_repo_sync = lambda: 0
check("stale check looks at the board's config", "aarch64" in (runner.stale_reason(plan) or ""),
      runner.stale_reason(plan))
try:
    targets.get("nope")
    check("unknown target raises", False)
except KeyError:
    check("unknown target raises", True)
check("old plans without a target are this computer",
      targets.of_plan({"generated": "x"}) is targets.THIS_COMPUTER)
sys.exit(1 if failed else 0)
