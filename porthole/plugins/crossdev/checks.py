"""
    Porthole crossdev plugin: is this computer ready to build for another one?

    Read-only checks (no root needed) of what building a system for another
    architecture takes here: crossdev and its repository, a cross toolchain
    for the target, qemu's user-mode emulator and binfmt (for what won't
    cross-compile), and distcc (so the emulated builds compile with the
    native cross compiler). Each check says what it found and, if
    something is missing, what to do about it; where Porthole can do it,
    the check's action says how:

      {"queue": {"name", "targets", "requested"}}   an install, planned
                                                    and reviewed like any
      {"root": [command words]}                     plugin crossdev ...

    qemu's binfmt registration and distcc's daemon aren't left running:
    the runner starts them for the jobs that need them (updater/ondemand.py).

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import glob
import gzip
import os
import re
import shutil
import subprocess

ROOTS_DIR = "/var/lib/porthole/roots"
REPO_NAME = "crossdev"
REPO_DIR = "/var/db/repos/crossdev"
# portage arch -> qemu's user-mode target name
QEMU_TARGETS = {"arm64": "aarch64", "arm": "arm", "riscv": "riscv64", "ppc64": "ppc64",
                "ppc": "ppc", "x86": "i386", "amd64": "x86_64", "loong": "loongarch64",
                "mips": "mips", "s390": "s390x", "sparc": "sparc64", "alpha": "alpha",
                "hppa": "hppa", "m68k": "m68k"}


class Check:
    """One thing checked: ok (True/False/None for unknown), what was found,
    and what to do if it isn't ok"""

    def __init__(self, key, title, ok, found, fix=None, action=None):
        self.key, self.title, self.ok, self.found, self.fix = key, title, ok, found, fix
        self.action = action if ok is False else None

    def __repr__(self):
        return "Check(%s, %s)" % (self.key, self.ok)


def _installed(cp):
    """Installed versions of cat/pkg: [(pf, USE set)]"""
    found = []
    for path in glob.glob("/var/db/pkg/%s-[0-9]*" % cp):
        try:
            with open(os.path.join(path, "USE")) as f:
                use = set(f.read().split())
        except OSError:
            use = set()
        found.append((os.path.basename(path), use))
    return found


def kernel_option(name):
    """y, m, n (unset), or None if the running kernel's config can't be read"""
    for path in ("/proc/config.gz", "/boot/config-%s" % os.uname().release):
        try:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt") as f:
                for line in f:
                    if line.startswith(name + "="):
                        return line.split("=", 1)[1].strip()
            return "n"
        except OSError:
            continue
    return None


def repos(portage_dir="/etc/portage"):
    """{name: location} of the repositories in repos.conf, read afresh
    (portage's own settings are read once per process, and the window
    checks again after adding one)"""
    import configparser
    from porthole.updater import system
    found = {}
    for name, path in system.repos_conf_sections(portage_dir).items():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path)
            found[name] = parser.get(name, "location", fallback="").strip()
        except configparser.Error:
            continue
    return found


def queue_install(name, targets, requested=()):
    return {"queue": {"name": name, "targets": list(targets),
                      "requested": [{"kind": k, "line": l} for k, l in requested]}}


def check_all(arch=None, tuple_=None):
    """Checks for building for arch (portage's name, e.g. arm64), with the
    toolchain tuple_ (e.g. aarch64-unknown-linux-gnu) if known"""
    checks = []

    # crossdev itself, and the repository it puts its toolchain ebuilds in
    have = _installed("sys-devel/crossdev")
    checks.append(Check(
        "crossdev", "crossdev", bool(have),
        "installed (%s)" % have[0][0] if have else "not installed",
        None if have else "Install sys-devel/crossdev.",
        queue_install("crossdev", ["sys-devel/crossdev"])))
    found = repos()
    cross_repo = next((n for n, loc in found.items()
                       if n == REPO_NAME or loc.rstrip("/").endswith("/" + REPO_NAME)), None)
    checks.append(Check(
        "crossdev-repo", "A repository for crossdev's toolchain packages",
        cross_repo is not None,
        "%s (%s)" % (cross_repo, found[cross_repo]) if cross_repo else "none",
        None if cross_repo else
        "crossdev puts the toolchain ebuilds it makes in a repository of their own; "
        "Porthole can create one in %s (local only, never synced)." % REPO_DIR,
        {"root": ["make-repo"]}))

    # the cross toolchain for the target
    if tuple_:
        gcc = shutil.which(tuple_ + "-gcc")
        checks.append(Check(
            "toolchain", "Cross toolchain for %s" % tuple_, bool(gcc),
            gcc or "not built",
            None if gcc else "Build it with crossdev, with the versions the target's "
            "profile allows (Porthole works them out)."))

    # qemu user mode, for what won't cross-compile
    # from the toolchain tuple when known (it tells the byte order: ppc64le,
    # mipsel, ...), the same name the build runs; else from portage's arch
    if tuple_:
        from porthole.plugins.crossdev.build import qemu_name
        target = qemu_name(tuple_)[len("qemu-"):]
    else:
        target = QEMU_TARGETS.get(arch or "")
    qemu = _installed("app-emulation/qemu")
    use = qemu[0][1] if qemu else set()
    static = "static-user" in use
    has_target = (not target) or ("qemu_user_targets_%s" % target) in use
    flags = ["static-user"] + (["qemu_user_targets_%s" % target] if target else [])
    if not qemu:
        # only user mode is needed: not the default full-system emulator,
        # with its firmware (an installed qemu keeps what it has)
        flags.append("-qemu_softmmu_targets_x86_64")
    checks.append(Check(
        "qemu", "qemu's user-mode emulator%s" % (" for %s" % target if target else ""),
        bool(qemu) and static and has_target,
        ("installed, USE " + " ".join(sorted(
            f for f in use if f == "static-user" or f.startswith("qemu_user_targets_"))))
        if qemu else "not installed",
        None if qemu and static and has_target else
        "Needed only for packages that won't cross-compile: app-emulation/qemu with "
        "USE=static-user and QEMU_USER_TARGETS=\"%s\"%s." % (
            target or "...", "" if qemu else ", user mode only"),
        queue_install("qemu (%s)" % (target or "user mode"), ["app-emulation/qemu"],
                      [("use", "app-emulation/qemu " + " ".join(flags))])))

    # binfmt: the kernel runs the target's programs through qemu
    option = kernel_option("CONFIG_BINFMT_MISC")
    checks.append(Check(
        "binfmt-kernel", "Kernel support for running other architectures' programs",
        option in ("y", "m") if option is not None else None,
        "CONFIG_BINFMT_MISC=%s" % option if option else "unknown (no kernel config readable)",
        None if option in ("y", "m") else "Set CONFIG_BINFMT_MISC in the kernel."))
    if target:
        from porthole.updater.ondemand import Binfmt
        how = Binfmt.available()
        entry = "/proc/sys/fs/binfmt_misc/qemu-%s" % target
        registered = os.path.exists(entry)
        checks.append(Check(
            "binfmt-qemu", "qemu can be registered to run %s programs" % target,
            bool(how) or registered,
            ("registered now" if registered else
             "Porthole starts qemu-binfmt for the builds that need it, and stops it "
             "after" if how == "openrc" else
             "systemd-binfmt registers qemu at boot" if how == "systemd" else
             "qemu's binfmt service isn't installed"),
            None if how or registered else "It comes with qemu (above)."))

    # distcc: emulated builds hand their compiling to the native cross
    # compiler, through a distccd the runner starts for the job on 127.0.0.1
    distcc = _installed("sys-devel/distcc")
    running = subprocess.run(["pgrep", "-x", "distccd"], capture_output=True).returncode == 0
    allows = _distccd_allows()
    checks.append(Check(
        "distcc", "distcc for builds in the emulator", bool(distcc),
        ("installed. Porthole starts a distccd of its own for each build in the "
         "emulator (on 127.0.0.1 only) and stops it after. Your distccd service "
         "(%s, allows %s) is left as it is." % ("running" if running else "not running",
                                               " ".join(allows) or "nobody"))
        if distcc else "not installed",
        None if distcc else "Install sys-devel/distcc.",
        queue_install("distcc", ["sys-devel/distcc"])))

    # room for the systems built
    base = ROOTS_DIR if os.path.isdir(ROOTS_DIR) else os.path.dirname(ROOTS_DIR)
    while not os.path.isdir(base):
        base = os.path.dirname(base)
    usage = shutil.disk_usage(base)
    free_gb = usage.free / 1024 ** 3
    checks.append(Check(
        "space", "Room for systems in %s" % ROOTS_DIR, free_gb >= 20,
        "%.0f GB free" % free_gb,
        None if free_gb >= 20 else
        "A desktop system takes 15-30 GB to build; free some space first."))
    return checks


def _distccd_allows():
    """Addresses /etc/conf.d/distccd lets in (--allow)"""
    try:
        with open("/etc/conf.d/distccd") as f:
            text = f.read()
    except OSError:
        return []
    opts = " ".join(re.findall(r'^\s*DISTCCD_OPTS="([^"]*)"', text, re.M))
    opts = opts.replace("${DISTCCD_OPTS}", "")
    return re.findall(r"--allow[= ]\s*(\S+)", opts)
