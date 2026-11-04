"""
    Porthole crossdev plugin: building in the system itself, under qemu

    For packages that won't cross-compile: the build runs inside the
    system's root (chroot), its programs run through qemu's user-mode
    emulator (binfmt), and its compiling is handed to this computer's
    cross compiler over distcc (the job's own distccd on 127.0.0.1), so
    only linking and the build's own tools run emulated.

    While it runs, the root gets: /proc, /sys and /dev, its repositories
    (read only) and the shared downloads and packages folders at the
    paths its config uses, this computer's resolv.conf (downloads), and
    the static qemu programs binfmt runs (at /usr/bin/qemu-<cpu> inside,
    where binfmt looks without its F flag). Everything is taken away again
    afterwards, whatever happened.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import re
import shutil
import subprocess

# what the root needs to build in itself, cross-built first when missing:
# its linker, distcc to hand compiling here, and portage itself (with
# Python and the rest it needs). Right after Start over the first package
# that won't cross-compile can come before @system brought portage in,
# and emerge wasn't there to run in the chroot. (Its compiler comes with
# @system before anything can fail.)
NEEDED = ("sys-devel/binutils", "sys-devel/distcc", "sys-apps/portage")
_FAILED = re.compile(r"\(([A-Za-z0-9+_.-]+/[A-Za-z0-9+_.:/-]+?)(?::[^,]*)?(?:::[A-Za-z0-9_-]+)?, "
                     r"(?:ebuild|binary) scheduled for merge to '([^']+)'\)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


class EmulateError(Exception):
    pass


def failed_packages(text, root):
    """[=cat/pkg-ver] emerge --keep-going listed as failed for root, from
    its output (the last list in text): only those that really failed (a
    build log follows them), not those dropped because a failed one is
    their dependency; those cross-compile once it is in"""
    text = _ANSI.sub("", text)
    marks = [m.end() for m in re.finditer(r"The following \d* ?packages? ha(?:s|ve) failed to "
                                          r"build", text)]
    if not marks:
        return []
    root = os.path.realpath(root).rstrip("/")
    found = []
    block = text[marks[-1]:]
    for match in _FAILED.finditer(block):
        cpv, where = match.group(1), match.group(2)
        if not block[match.end():match.end() + 12].lstrip(" ,").startswith("Log file"):
            continue  # dropped: never built
        cpv = re.sub(r":.*$", "", cpv)
        if os.path.realpath(where).rstrip("/") == root and "=" + cpv not in found:
            found.append("=" + cpv)
    return found


_CYCLE_FIX = re.compile(r"^\s*\*?\s*- ([A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+?)(?::\S+)?"
                        r" \(Change USE: ([^)]+)\)", re.M)


def cycle_suggestions(text):
    """[(cpv, "flags")] emerge suggested for breaking a circular dependency
    ("It might be possible to break this cycle by applying ... - sys-libs/
    pam-1.7.2 (Change USE: -elogind)"), in its order, from the last such
    message in text"""
    text = _ANSI.sub("", text)
    start = text.rfind("break this cycle")
    if start < 0:
        return []
    return [(m.group(1), " ".join(m.group(2).split()))
            for m in _CYCLE_FIX.finditer(text[start:start + 4000])]


def installed(root, cp):
    import glob
    return bool(glob.glob(os.path.join(root, "var/db/pkg", cp + "-[0-9]*")))


class Chroot:
    """The mounts a build inside root needs; a context manager that takes
    them all away again"""

    def __init__(self, target, log=lambda m: None):
        self.target, self.log = target, log
        self.root = os.path.realpath(target.root)
        self.mounted = []   # in mount order
        self.created = []   # placeholder files to remove

    def _run(self, argv):
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            raise EmulateError("%s: %s" % (" ".join(argv), (result.stderr or
                                                          result.stdout).strip()))

    def _inside(self, path):
        inside = os.path.join(self.root, path.lstrip("/"))
        if not os.path.realpath(inside).startswith(self.root + "/"):
            raise EmulateError("%s leads out of the system's folder" % path)
        return inside

    def _bind(self, source, path, read_only=False, recursive=False, is_file=False):
        point = self._inside(path)
        if is_file:
            if not os.path.lexists(point):
                os.makedirs(os.path.dirname(point), exist_ok=True)
                open(point, "w").close()
                self.created.append(point)
        else:
            os.makedirs(point, exist_ok=True)
        self._run(["mount", "--rbind" if recursive else "--bind", source, point])
        self.mounted.append(point)
        if recursive:
            self._run(["mount", "--make-rslave", point])
        if read_only:
            self._run(["mount", "-o", "remount,bind,ro", point])

    def __enter__(self):
        from porthole.plugins.crossdev import build, conf, roots
        try:
            point = self._inside("/proc")
            os.makedirs(point, exist_ok=True)
            self._run(["mount", "-t", "proc", "proc", point])
            self.mounted.append(point)
            self._bind("/sys", "/sys", recursive=True)
            self._bind("/dev", "/dev", recursive=True)
            point = self._inside("/run")
            os.makedirs(point, exist_ok=True)
            self._run(["mount", "-t", "tmpfs", "-o", "mode=755", "tmpfs", point])
            self.mounted.append(point)
            for name, location in sorted(roots.used_repos(self.target).items()):
                self._bind(location, location, read_only=True)
            info = conf.read(self.target)
            for name in ("DISTDIR", "PKGDIR"):
                folder = conf.effective(info, name)
                if folder and os.path.isabs(folder):
                    os.makedirs(folder, mode=0o755, exist_ok=True)
                    self._bind(folder, folder)
            # downloads need name lookups; a resolv.conf that is a link (into
            # /run, say) is left alone: binding over it would land outside
            if os.path.exists("/etc/resolv.conf") and not os.path.islink(
                    os.path.join(self.root, "etc/resolv.conf")):
                self._bind(os.path.realpath("/etc/resolv.conf"), "/etc/resolv.conf",
                           read_only=True, is_file=True)
            for ident in build.dynamic_linkers(self.root):
                qemu = build.qemu_for(ident)
                host = shutil.which("qemu-" + qemu) if qemu else None
                if host:
                    self._bind(host, "/usr/bin/qemu-" + qemu, read_only=True, is_file=True)
        except Exception:
            self.__exit__(None, None, None)
            raise
        self.log("== emulator: %s ready (%d mounts)" % (self.root, len(self.mounted)))
        return self

    def __exit__(self, *exc):
        problems = []
        for point in reversed(self.mounted):
            result = subprocess.run(["umount", "-R", point], capture_output=True, text=True)
            if result.returncode != 0:
                # busy (a process left behind): detach it rather than keep it
                lazy = subprocess.run(["umount", "-R", "-l", point], capture_output=True,
                                      text=True)
                if lazy.returncode != 0:
                    problems.append(point)
        self.mounted = []
        for path in reversed(self.created):
            try:
                os.unlink(path)
            except OSError:
                pass
        self.created = []
        if problems:
            self.log("!! couldn't unmount: %s" % ", ".join(problems))
        else:
            self.log("== emulator: mounts taken away")
        return False

    def emerge_argv(self, args, env):
        """argv running emerge inside the root, with a clean environment.
        Never another Python: the system's Python is its profile's, and a
        build tool's "any Python 3.x" would otherwise pull in a new slot
        (seen: python 3.14 next to the profile's 3.13, compiled under qemu
        for nothing)"""
        return chroot_argv(self.root, ["emerge", "--exclude", "dev-lang/python"] + list(args),
                           env)


def chroot_argv(root, command, env=None):
    """argv running command inside root with a clean environment (LANG=C:
    no locales are generated in a new system)"""
    keep = ["HOME=/root", "TERM=xterm", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C"]
    keep += ["%s=%s" % (k, v) for k, v in sorted((env or {}).items())]
    return ["chroot", os.path.realpath(root), "/usr/bin/env", "-i"] + keep + list(command)


def ldconfig(runner, job_id, step, log, target):
    """The system's library cache (/etc/ld.so.cache), made by its own
    ldconfig: portage can't run it while cross-building, and without it
    nothing finds libraries outside the default folders (gcc's libgcc_s,
    for one), in the emulator or when the system boots. Runs under qemu
    (binfmt) when the system is for another CPU."""
    from porthole.plugins.crossdev import build
    from porthole.updater.ondemand import Helpers, HelperError
    root = os.path.realpath(target.root)
    if not any(os.path.exists(os.path.join(root, d, "ldconfig"))
               for d in ("usr/bin", "usr/sbin", "sbin")):
        log("No ldconfig in the system yet (sys-libs/glibc); library cache not made.")
        return True
    chost = build.chost_of(target)
    helpers = Helpers(job_id, ["binfmt"] if chost != build.host_chost() else [])
    try:
        helpers.start(log)
    except HelperError as e:
        log("!! %s" % e)
        return False
    try:
        # binfmt finds qemu at its own path inside the root (no F flag)
        placeholder = None
        for ident in build.dynamic_linkers(root):
            qemu = build.qemu_for(ident)
            host = shutil.which("qemu-" + qemu) if qemu else None
            inside = os.path.join(root, "usr/bin", "qemu-" + qemu) if qemu else None
            if host and chost != build.host_chost() and not os.path.lexists(inside):
                shutil.copy2(host, inside)
                placeholder = placeholder or []
                placeholder.append(inside)
        argv = chroot_argv(root, ["ldconfig"])
        log("$ " + " ".join(argv))
        ok = runner.run_logged(job_id, step, log, argv)
        if ok:
            log("Made the system's library cache (/etc/ld.so.cache)")
        return ok
    finally:
        for path in placeholder or []:
            try:
                os.unlink(path)
            except OSError:
                pass
        helpers.stop(log)


def merge_list(text):
    """[=cat/pkg-ver] of an emerge --pretend's merge list (ebuilds only)"""
    found = []
    for line in _ANSI.sub("", text).splitlines():
        match = re.match(r"^\[ebuild[^\]]*\]\s+([A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+?)(?::\S*)?"
                         r"(?:::\S+)?(?:\s|$)", line)
        if match:
            atom = "=" + match.group(1)
            if atom not in found:
                found.append(atom)
    return found


def cp_of(atom):
    """cat/pkg of =cat/pkg-ver"""
    from portage.versions import cpv_getkey
    return cpv_getkey(atom.lstrip("="))


_COMPILER = re.compile(r"^(?:[a-z0-9_]+(?:-[a-z0-9_.]+){2,4}-)?(gcc|g\+\+|cc|c\+\+)(-\d+)?$")


def distcc_links(root, log=lambda m: None):
    """distcc's links in the system's /usr/lib/distcc (bin is a link to
    it), one per compiler it has, as distcc-config --update-masquerade
    makes them on the system itself: a cross-installed distcc can't, and
    without them FEATURES=distcc compiles everything locally (here: under
    qemu). Plain gcc/cc are fine too: distcc rewrites them to the full
    CHOST name before sending. Returns how many there are."""
    root = os.path.realpath(root)
    if not os.path.exists(os.path.join(root, "usr/bin/distcc")):
        return 0
    folder = os.path.join(root, "usr/lib/distcc")
    os.makedirs(folder, mode=0o755, exist_ok=True)
    made = 0
    for name in sorted(os.listdir(os.path.join(root, "usr/bin"))):
        if not _COMPILER.match(name):
            continue
        link = os.path.join(folder, name)
        if os.path.islink(link) and os.readlink(link) == "/usr/bin/distcc":
            made += 1
            continue
        if os.path.lexists(link):
            continue
        os.symlink("/usr/bin/distcc", link)
        made += 1
    if made:
        log("== distcc: %d compiler names in the system go to distcc" % made)
    return made


def emulated_env(target, helper_env):
    """The environment of emerge inside the root: distcc to the job's
    distccd when the root has distcc; no sandboxes that don't work under
    qemu or keep distcc from reaching 127.0.0.1"""
    root = os.path.realpath(target.root)
    features = ["-pid-sandbox", "-network-sandbox"]
    env = {}
    if installed(root, "sys-devel/distcc") and helper_env.get("DISTCC_HOSTS"):
        features.insert(0, "distcc")
        env["DISTCC_HOSTS"] = helper_env["DISTCC_HOSTS"]
    if helper_env.get("MAKEOPTS"):
        env["MAKEOPTS"] = helper_env["MAKEOPTS"]
    env["FEATURES"] = " ".join(features)
    return env
