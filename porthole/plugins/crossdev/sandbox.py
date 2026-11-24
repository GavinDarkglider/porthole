"""
    Porthole crossdev plugin: keeping a cross build off this computer

    A cross build installs into the system's folder (ROOT), but its ebuilds
    run on this computer, as root. A package that writes outside ROOT (a
    user added to this computer's /etc/passwd, a file put in /usr) would
    change this computer without anyone asking. So every emerge into the
    system runs in a mount namespace of its own, where /etc, /usr and /var
    are overlays: it sees this computer's files, but what it writes there
    goes to a scratch layer, not to them. What it really has to write goes
    through: the system's folder, its settings, portage's build and
    download folders, its binary packages, ccache, logs and portage's
    cache (KEEP). Nothing else on this computer sees the overlays.

    When the emerge ends, the scratch layer is looked at: empty is the
    normal case. Anything in it (besides scratch files, IGNORE) is a write
    to this computer that the build wasn't allowed to make: the step
    fails, the log lists the files, and a copy of them is kept in
    /var/lib/porthole/sandbox/<job>/ for looking at. Nothing is copied
    back; a crash leaves this computer as it was.

    Tools a build runs here (BDEPEND, IDEPEND: an acct-user package when a
    build sets file owners by name) are installed here, but in a step of
    their own, before the emerge, and only those the user agreed to when
    the build was planned (root.py).

    Run as root: python -m porthole.plugins.crossdev.sandbox --report FILE
        [--keep PATH]... -- COMMAND ARGS

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

TREES = ("/etc", "/usr", "/var")
SAVE_DIR = "/var/lib/porthole/sandbox"
# written by every build, and nobody's business: scratch and caches
IGNORE = ("/var/tmp/", "/var/cache/edb/dep/")
IGNORE_PARTS = ("/__pycache__/",)
# what the launcher exits with when it couldn't set the overlays up
SETUP_FAILED = 125
STASH_PARENT = "/run"


def _user_namespace():
    """Running as root of a user namespace (a container): overlays then keep
    their notes in user.* attributes"""
    try:
        with open("/proc/self/uid_map") as f:
            return f.read().split() != ["0", "0", "4294967295"]
    except OSError:
        return False


class SandboxError(Exception):
    pass


class Stopped(Exception):
    """The build tried to change this computer (already logged): the step
    fails as it is, without the usual retries"""


def _tree_of(path):
    for tree in TREES:
        if path == tree or path.startswith(tree + "/"):
            return tree
    return None


def keep_paths(target, env, run=subprocess.run):
    """Folders the emerge writes for real: the system's own, and portage's
    build, download, package and cache folders as the system's settings
    give them (asked of portage, not guessed)"""
    keep = [os.path.realpath(target.root), os.path.realpath(target.config_root),
            "/var/log", "/var/cache/edb"]
    names = ("PORTAGE_TMPDIR", "DISTDIR", "PKGDIR", "CCACHE_DIR", "PORT_LOGDIR")
    result = run(["portageq", "envvar", "-v"] + list(names), env=dict(os.environ, **env),
                 capture_output=True, text=True)
    for line in result.stdout.splitlines():
        name, _sep, value = line.partition("=")
        value = value.strip().strip("'\"")
        if name not in names or not os.path.isabs(value):
            continue
        # PORTAGE_TMPDIR is the folder portage makes its own "portage" in
        keep.append(os.path.join(value, "portage") if name == "PORTAGE_TMPDIR" else value)
    out = []
    for path in keep:
        path = os.path.normpath(path)
        if path not in out:
            out.append(path)
    return out


def launcher_argv(argv, keep, report):
    return ([sys.executable, "-m", "porthole.plugins.crossdev.sandbox", "--report", report]
            + [a for path in keep for a in ("--keep", path)] + ["--"] + list(argv))


def run(runner, job_id, step, log, argv, target, env):
    """argv (an emerge into the system) in the sandbox: whether it worked.
    Raises Stopped when it wrote to this computer outside what it may, or
    the sandbox couldn't be set up"""
    keep = keep_paths(target, env)
    folder = os.path.join(SAVE_DIR, job_id)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, report = tempfile.mkstemp(prefix="report-", suffix=".json", dir=folder)
    os.close(fd)
    try:
        ok = runner.run_logged(job_id, step, log, launcher_argv(argv, keep, report),
                               extra_env=env)
        try:
            with open(report) as f:
                found = json.load(f)
        except (OSError, ValueError):
            found = {"error": "the sandbox left no report"}
    finally:
        for path, remove in ((report, os.unlink), (folder, os.rmdir)):
            try:
                remove(path)   # the folder only when nothing was kept in it
            except OSError:
                pass
    if found.get("error"):
        log("!! The build wasn't kept off this computer, so it didn't run: %s"
            % found["error"])
        raise Stopped()
    changes = found.get("changes") or []
    if changes:
        log("!! The build tried to change this computer outside the system's folder. "
            "Those changes were not made; this step failed so you can look at them:")
        for kind, path in changes[:50]:
            log("     %-8s %s" % (kind, path))
        if len(changes) > 50:
            log("     ... and %d more" % (len(changes) - 50))
        if found.get("saved"):
            log("   A copy of what it wrote is in %s" % found["saved"])
        raise Stopped()
    return ok


# -- the launcher (runs as root, in its own mount namespace) --------------------------

def _mount(*args):
    result = subprocess.run(["mount"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise SandboxError("mount %s: %s" % (" ".join(args), result.stderr.strip()))


def _submounts():
    """Mount points under TREES, outermost first (overlays don't show them)"""
    points = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            point = line.split()[4].replace("\\040", " ")
            if point not in TREES and _tree_of(point) and point not in points:
                points.append(point)
    return sorted(points, key=lambda p: p.count("/"))


class Sandbox:
    def __init__(self, keep):
        self.keep = sorted({os.path.normpath(p) for p in keep if _tree_of(p)},
                           key=lambda p: p.count("/"))
        self.stash = None
        self.bound = set()   # trees reachable as they are under the stash
        self.userxattr = _user_namespace()

    def real(self, path):
        """path on this computer itself, seen from inside the overlays"""
        tree = _tree_of(path)
        return os.path.join(self.stash, "real" + path) if tree in self.bound else path

    def top(self, tree):
        return os.path.join(self.stash, "top" + tree, "upper")

    def setup(self):
        os.unshare(os.CLONE_NEWNS)
        _mount("--make-rprivate", "/")
        if "overlay" not in open("/proc/filesystems").read():
            subprocess.run(["modprobe", "overlay"], capture_output=True)
        # /run is left as it is (resolv.conf may point into it)
        self.stash = tempfile.mkdtemp(prefix="porthole-sandbox-", dir=STASH_PARENT)
        _mount("-t", "tmpfs", "-o", "mode=700,size=25%", "tmpfs", self.stash)
        submounts = _submounts()
        for tree in TREES:
            # this computer's own trees, for what is kept writable
            point = os.path.join(self.stash, "real" + tree)
            os.makedirs(point)
            _mount("--rbind", tree, point)
            self.bound.add(tree)
        for path in self.keep:
            # made on this computer itself, so the overlay shows the folder
            os.makedirs(self.real(path), mode=0o755, exist_ok=True)
        for tree in TREES:
            upper, work = self.top(tree), os.path.join(self.stash, "top" + tree, "work")
            os.makedirs(upper)
            os.makedirs(work)
            _mount("-t", "overlay", "overlay", "-o",
                   "lowerdir=%s,upperdir=%s,workdir=%s%s" % (
                       tree, upper, work, ",userxattr" if self.userxattr else ""), tree)
        for point in submounts:
            # other filesystems under them: there, read-only
            if any(point == k or point.startswith(k + "/") for k in self.keep):
                continue
            _mount("--bind", self.real(point), point)
            _mount("-o", "remount,bind,ro", point)
        for path in self.keep:
            _mount("--rbind", self.real(path), path)

    def changes(self):
        """[(kind, path)] the command wrote to the overlays"""
        found = []
        for tree in TREES:
            upper = self.top(tree)
            for folder, dirs, files in os.walk(upper):
                rel = folder[len(upper):]
                for name in dirs + files:
                    path = tree + rel + "/" + name
                    here = os.path.join(folder, name)
                    st = os.lstat(here)
                    if path.startswith(IGNORE) or any(p in path + "/" for p in IGNORE_PARTS):
                        continue
                    real = self.real(path)
                    if stat.S_ISCHR(st.st_mode) and st.st_rdev == 0:
                        found.append(("removed", path))
                    elif stat.S_ISDIR(st.st_mode):
                        opaque = _xattr(here, "%s.overlay.opaque" % (
                            "user" if self.userxattr else "trusted")) == b"y"
                        if not os.path.isdir(real):
                            if not os.listdir(here):
                                found.append(("added", path + "/"))
                        elif opaque:
                            found.append(("replaced", path + "/"))
                    else:
                        found.append(("changed" if os.path.lexists(real) else "added", path))
        return found

    def save(self, report):
        """A copy of the overlays' files next to the report (removals listed)"""
        folder = os.path.splitext(report)[0]
        target = self.real(folder)
        for tree in TREES:
            upper = self.top(tree)
            if os.listdir(upper):
                shutil.copytree(upper, target + tree, symlinks=True,
                                ignore=lambda d, names: [n for n in names if not _copyable(
                                    os.path.join(d, n))])
        return folder

    def write_report(self, report, data):
        path = self.real(report)
        with open(path + ".new", "w") as f:
            json.dump(data, f)
        os.replace(path + ".new", path)


def _xattr(path, name):
    try:
        return os.getxattr(path, name, follow_symlinks=False)
    except OSError:
        return None


def _copyable(path):
    """Files, folders and links (not removals, devices or pipes)"""
    mode = os.lstat(path).st_mode
    return stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)


def main(args):
    keep, report = [], None
    while args and args[0] != "--":
        if args[0] == "--keep" and len(args) > 1:
            keep.append(args[1])
            args = args[2:]
        elif args[0] == "--report" and len(args) > 1:
            report = args[1]
            args = args[2:]
        else:
            print("usage: sandbox --report FILE [--keep PATH]... -- COMMAND", file=sys.stderr)
            return 2
    command = args[1:]
    if not command or not report or os.geteuid() != 0:
        print("usage (as root): sandbox --report FILE [--keep PATH]... -- COMMAND",
              file=sys.stderr)
        return 2
    sandbox = Sandbox(keep)
    try:
        sandbox.setup()
    except (SandboxError, OSError) as e:
        try:
            # setup can fail before the overlay over /var is up: then the
            # report path is this computer's own
            sandbox.write_report(report, {"error": str(e)})
        except OSError:
            pass
        print("!! %s" % e, file=sys.stderr)
        if sandbox.stash:
            subprocess.run(["umount", "-R", sandbox.stash], capture_output=True)
            try:
                os.rmdir(sandbox.stash)
            except OSError:
                pass
        return SETUP_FAILED
    # the runner stops the whole process group: the command stops, this
    # waits for it and still reports
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    pid = os.fork()
    if pid == 0:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_DFL)
        try:
            os.execvp(command[0], command)
        except OSError as e:
            print("!! %s: %s" % (command[0], e), file=sys.stderr)
        os._exit(127)
    while True:
        try:
            _pid, status = os.waitpid(pid, 0)
            break
        except ChildProcessError:
            status = 1 << 8
            break
        except InterruptedError:
            continue
    code = os.waitstatus_to_exitcode(status)
    data = {"code": code}
    try:
        data["changes"] = sandbox.changes()
        if data["changes"]:
            data["saved"] = sandbox.save(report)
    except OSError as e:
        data["error"] = "couldn't look at what the build wrote: %s" % e
    sandbox.write_report(report, data)
    # the namespace and its mounts go with this process; the stash folder
    # itself is on this computer's /run
    subprocess.run(["umount", "-R", sandbox.stash], capture_output=True)
    try:
        os.rmdir(sandbox.stash)
    except OSError:
        pass
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
