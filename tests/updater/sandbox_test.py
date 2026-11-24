"""
    Keeping cross builds off this computer (plugins/crossdev/sandbox.py),
    without root: the launcher runs as root of a user namespace (unshare -r),
    so its overlays are private to the test.

        python3 tests/updater/sandbox_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from porthole.plugins.crossdev import build, health, sandbox  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, (": %s" % detail) if detail and not ok
                       else ""))
    failed += not ok


LAUNCH = """
import sys
sys.path.insert(0, %r)
from porthole.plugins.crossdev import sandbox
sandbox.STASH_PARENT = sys.argv[1]
sys.exit(sandbox.main(sys.argv[2:]))
""" % os.path.dirname(os.path.dirname(HERE))


def launcher(scratch, keep, command):
    stash = os.path.join(scratch, "stash")
    os.makedirs(stash, exist_ok=True)
    report = os.path.join(scratch, "report.json")
    argv = ["unshare", "-r", "--", sys.executable, "-c", LAUNCH, stash, "--report", report]
    for path in keep:
        argv += ["--keep", path]
    result = subprocess.run(argv + ["--", "sh", "-c", command], capture_output=True, text=True)
    with open(report) as f:
        return result.returncode, json.load(f), os.listdir(stash)


def test_launcher():
    with tempfile.TemporaryDirectory(dir="/var/tmp") as kept, \
            tempfile.TemporaryDirectory() as scratch:
        name = os.path.basename(kept)
        code, report, stash = launcher(scratch, [kept], (
            "touch /etc/porthole-sandbox-test /usr/porthole-sandbox-test %s/real; "
            "mkdir /var/porthole-sandbox-test; exit 3" % kept))
        check("command's exit code passed on", code == 3, code)
        changes = sorted(tuple(c) for c in report.get("changes", ()))
        check("writes to /etc, /usr, /var caught", changes == [
            ("added", "/etc/porthole-sandbox-test"), ("added", "/usr/porthole-sandbox-test"),
            ("added", "/var/porthole-sandbox-test/")], changes)
        check("nothing reached this computer", not any(os.path.lexists(p) for p in (
            "/etc/porthole-sandbox-test", "/usr/porthole-sandbox-test",
            "/var/porthole-sandbox-test")))
        check("kept folder written for real", os.path.exists(os.path.join(kept, "real")))
        check("caught files saved", os.path.exists(os.path.join(
            report.get("saved", ""), "etc/porthole-sandbox-test")), report.get("saved"))
        check("stash cleaned up", stash == [], stash)
        check("kept folder not reported", not any(name in c[1] for c in changes))

        code, report, _stash = launcher(scratch, [kept], "cat /etc/hostname >/dev/null")
        check("a clean run reports nothing", code == 0 and report.get("changes") == [],
              report)


class FakeRunner:
    def __init__(self, report):
        self.report, self.argv = report, None

    def run_logged(self, job_id, step, log, argv, extra_env=None):
        self.argv = argv
        path = argv[argv.index("--report") + 1]
        with open(path, "w") as f:
            json.dump(self.report, f)
        return self.report.get("code") == 0


def test_run():
    target = types.SimpleNamespace(root="/nonexistent-root", config_root="/nonexistent-root")
    old_save, old_keep = sandbox.SAVE_DIR, sandbox.keep_paths
    with tempfile.TemporaryDirectory() as scratch:
        sandbox.SAVE_DIR = scratch
        sandbox.keep_paths = lambda target, env: ["/nonexistent-root"]
        try:
            lines = []
            ok = sandbox.run(FakeRunner({"code": 0, "changes": []}), "job", {}, lines.append,
                             ["emerge", "x"], target, {})
            check("clean emerge passes", ok is True)
            runner = FakeRunner({"code": 0, "changes": [["added", "/etc/passwd"]]})
            try:
                sandbox.run(runner, "job", {}, lines.append, ["emerge", "x"], target, {})
                check("write to this computer stops the step", False)
            except sandbox.Stopped:
                check("write to this computer stops the step", True)
            check("caught file logged", any("/etc/passwd" in l for l in lines), lines)
            check("emerge runs behind the launcher",
                  runner.argv[-2:] == ["emerge", "x"] and "--keep" in runner.argv)
            try:
                sandbox.run(FakeRunner({"error": "no overlayfs"}), "job", {}, lines.append,
                            ["emerge", "x"], target, {})
                check("no sandbox, no build", False)
            except sandbox.Stopped:
                check("no sandbox, no build", True)
        finally:
            sandbox.SAVE_DIR, sandbox.keep_paths = old_save, old_keep


def test_policy():
    target = types.SimpleNamespace(name="t")
    with tempfile.TemporaryDirectory() as scratch:
        build.write_policy(target, True, ["acct-user/messagebus-0-r3", "dev-libs/glib-2.84.4"],
                           base=scratch)
        portage = os.path.join(scratch, "etc-portage")
        os.makedirs(os.path.join(portage, "package.use"))
        with open(os.path.join(portage, "package.use", build.HOST_FILE % "t"), "w") as f:
            f.write("# comment\n>=dev-python/foo-1.2 python_targets_python3_13\n")
        approved = build.approved_host(target, portage, base=scratch)
        check("agreed tools and allowed Python changes approved", approved == {
            "acct-user/messagebus", "dev-libs/glib", "dev-python/foo"}, approved)
        try:
            build.write_policy(target, False, ["not a package; rm"], base=scratch)
            check("bad package refused", False)
        except build.BuildError:
            check("bad package refused", True)


def test_accounts():
    with tempfile.TemporaryDirectory() as host, tempfile.TemporaryDirectory() as root:
        for folder, passwd, group in (
                (host, "root:x:0:0::/root:/bin/sh\nmessagebus:x:101:101::/:/sbin/nologin\n",
                 "root:x:0:\nmessagebus:x:101:\n"),
                (root, "root:x:0:0::/root:/bin/sh\nmessagebus:x:105:101::/:/sbin/nologin\n",
                 "root:x:0:\nmessagebus:x:101:\nonly-there:x:7:\n")):
            os.makedirs(os.path.join(folder, "etc"))
            with open(os.path.join(folder, "etc/passwd"), "w") as f:
                f.write(passwd)
            with open(os.path.join(folder, "etc/group"), "w") as f:
                f.write(group)
        found = health.account_mismatches(root, host)
        check("differing account numbers found", found == [
            "user messagebus: 101 on this computer, 105 in the system"], found)


PRETEND = """
[ebuild  N     ] acct-group/messagebus-0-r3::gentoo  0 KiB
[ebuild  N     ] acct-user/messagebus-0-r3::gentoo  0 KiB
[ebuild  N     ] sys-apps/dbus-1.16.2::gentoo to /r/ USE="-X" 1 KiB
"""


def test_host_tools():
    from porthole.plugins.crossdev import root as root_steps
    target = types.SimpleNamespace(name="t", root="/r")
    calls = []

    class Runner:
        def run_logged(self, job_id, step, log, argv, extra_env=None):
            calls.append(argv)
            return True
    old_run, old_approved = root_steps.subprocess.run, build.approved_host
    root_steps.subprocess.run = lambda argv, **kw: types.SimpleNamespace(
        stdout=PRETEND, returncode=0)
    try:
        build.approved_host = lambda target: {"acct-group/messagebus", "acct-user/messagebus"}
        lines = []
        root_steps._host_tools(Runner(), "job", {}, lines.append, target, {},
                               ["emerge", "--color=y", "@world"])
        check("agreed tools installed here first, without the system's", calls == [[
            "emerge", "--verbose", "--color=y", "--oneshot", "=acct-group/messagebus-0-r3",
            "=acct-user/messagebus-0-r3"]], calls)
        build.approved_host = lambda target: {"acct-group/messagebus"}
        calls.clear()
        try:
            root_steps._host_tools(Runner(), "job", {}, lines.append, target, {},
                                   ["emerge", "@world"])
            check("tool not agreed to stops the build", False)
        except sandbox.Stopped:
            check("tool not agreed to stops the build", calls == [] and any(
                "acct-user/messagebus-0-r3" in l for l in lines), lines)
    finally:
        root_steps.subprocess.run, build.approved_host = old_run, old_approved


test_launcher()
test_host_tools()
test_run()
test_policy()
test_accounts()
print("%d failed" % failed)
sys.exit(1 if failed else 0)
