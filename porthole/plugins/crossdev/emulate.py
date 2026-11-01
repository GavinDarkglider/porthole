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

# what the root needs to build in itself (a compiler it has from @system)
NEEDED = ("sys-devel/binutils", "sys-devel/distcc")
_FAILED = re.compile(r"\(([A-Za-z0-9+_.-]+/[A-Za-z0-9+_.:/-]+?)(?::[^,]*)?(?:::[A-Za-z0-9_-]+)?, "
                     r"(?:ebuild|binary) scheduled for merge to '([^']+)'\)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


class EmulateError(Exception):
    pass


def failed_packages(text, root):
    """[=cat/pkg-ver] emerge --keep-going listed as failed for root, from
    its output (the last list in text)"""
    text = _ANSI.sub("", text)
    marks = [m.end() for m in re.finditer(r"The following \d* ?packages? ha(?:s|ve) failed to "
                                          r"build", text)]
    if not marks:
        return []
    root = os.path.realpath(root).rstrip("/")
    found = []
    for match in _FAILED.finditer(text[marks[-1]:]):
        cpv, where = match.group(1), match.group(2)
        cpv = re.sub(r":.*$", "", cpv)
        if os.path.realpath(where).rstrip("/") == root and "=" + cpv not in found:
            found.append("=" + cpv)
    return found


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
        """argv running emerge inside the root, with a clean environment"""
        keep = ["HOME=/root", "TERM=xterm", "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG=C.UTF-8"]
        keep += ["%s=%s" % (k, v) for k, v in sorted(env.items())]
        return ["chroot", self.root, "/usr/bin/env", "-i"] + keep + ["emerge"] + list(args)


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
