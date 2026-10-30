#!/usr/bin/env python3
"""
    Fully automatic updates (automation = auto) on the scenarios: what the
    autopilot accepts, what it holds back, and the news item it writes.

        python3 tests/updater/autopilot_scenarios.py [NAME...] [--news]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios  # noqa: E402
from porthole.updater import autopilot  # noqa: E402

# what the autopilot should come to: plan status, versions merged, and the
# choices it refused (held back) by itself
EXPECTED = {
    # temporary USE to break the cycles: verified, accepted
    "cycles": ("needs-approval", ["lib/x-2", "lib/y-2", "media/p-2", "media/q-2"], []),
    # keyword + USE change on a dependency: verified, accepted
    "fixes": ("needs-approval", ["app/tool-2", "lib/feat-1", "lib/k-3"], []),
    # replacing a world package: never by itself, the update waits
    "blocker": ("up-to-date", [], ["replace:app/oldtool"]),
    # removing an orphan that holds an update back: never by itself
    "old-dep": ("needs-help", [], ["remove:app-old/legacy"]),
    "held-stuck": ("needs-help", [], []),
    # a testing version that would free a held-back update: left for the user
    "held-newer": None,
}


def main(argv):
    names = [a for a in argv if not a.startswith("-")] or list(EXPECTED)
    failed = 0
    for name in names:
        base = scenarios.SCENARIOS[name]

        def analyze(choices, base=base):
            scenarios.SCENARIOS["_auto"] = dict(base, choices=choices)
            return scenarios.run("_auto", quiet=True)
        plan, refused = autopilot.plan_by_itself(analyze, base.get("choices") or {})
        got = (plan["status"], sorted(m["cpv"] for m in plan["merge_list"]), sorted(refused))
        want = EXPECTED.get(name)
        ok = want is None or got == want
        failed += not ok
        print("%s %-12s %s" % ("ok  " if ok else "FAIL", name, got))
        if not ok:
            print("     wanted", want)
        if "--news" in argv:
            title, body = autopilot.news_text(plan, refused, "01:00-06:00", "JOB")
            print("\n  " + title + "\n" + "\n".join("  | " + line for line in body.splitlines()))
            print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
