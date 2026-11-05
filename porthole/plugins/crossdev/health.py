#!/usr/bin/env python3

'''
    Porthole crossdev plugin: the health check of a finished system

    The last step of a build: is what was built really the system's? Each
    finding is an error (the system won't work as built: packaging stops)
    or a warning (worth a look, reported only):

      - programs and libraries for another CPU than the system's
        (this computer's: a cross build leaked into it)
      - libraries a program needs that the system doesn't have
      - perl and Python modules under this computer's names
      - binary packages of the system with this computer's names in them
      - kernel modules without their index (depmod)
      - files installed packages list that are gone, old libraries kept
        for programs not rebuilt yet, links that lead nowhere

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
'''

import glob
import json
import os
import shutil
import subprocess

ERROR, WARNING = "error", "warning"
REPORT_DIR = "/var/lib/porthole/health"

# not part of the running system, or not the system's own programs
SKIP = ("proc", "sys", "dev", "run", "tmp", "var/tmp", "var/cache", "var/db/repos",
        "var/db/pkg", "var/lib/portage")
# programs here may be for other CPUs (firmware for chips in the device),
# or are built for this computer on purpose (the kernel's build tools)
FOREIGN_OK = ("lib/firmware", "usr/lib/firmware", "usr/src", "usr/share")
# programs and libraries the system runs from its own folders
SYSTEM_DIRS = ("bin", "sbin", "lib", "lib32", "lib64", "usr/bin", "usr/sbin", "usr/lib",
               "usr/lib32", "usr/lib64", "usr/libexec")
SCAN = ("bin", "sbin", "lib", "lib32", "lib64", "usr", "opt", "etc")
LIB_DIRS = ("lib64", "usr/lib64", "lib", "usr/lib", "lib32", "usr/lib32")
# shown per kind of finding before "and N more"
SHOW = 15


class Finding:
    def __init__(self, severity, kind, title, items=(), fix=None):
        self.severity, self.kind, self.title = severity, kind, title
        self.items, self.fix = list(items), fix

    def as_dict(self):
        return {"severity": self.severity, "kind": self.kind, "title": self.title,
                "items": self.items, "fix": self.fix}


def _rel(root, path):
    return "/" + os.path.relpath(path, root)


def _under(rel, folders):
    rel = rel.lstrip("/")
    return any(rel == f or rel.startswith(f + "/") for f in folders)


# -- programs: their CPU and the libraries they need ----------------------------------

def scan_elf(root):
    """[(machine, path, needed list, rpath list)] of every program and
    library in the system (scanelf, from pax-utils), outside SKIP"""
    if not shutil.which("scanelf"):
        return None
    # where programs live (not var: the repositories alone are 100 000s of files)
    tops = [os.path.join(root, d) for d in SCAN
            if os.path.isdir(os.path.join(root, d)) and not os.path.islink(os.path.join(root, d))]
    if not tops:
        return []
    out = subprocess.run(["scanelf", "-R", "-B", "-F", "%a;%F;%n;%r"] + tops,
                         capture_output=True, text=True, errors="replace").stdout
    found, seen = [], set()
    for line in out.splitlines():
        parts = line.split(";")
        if len(parts) < 4:
            continue
        machine, path, needed, rpath = parts[0], parts[1], parts[2], ";".join(parts[3:])
        rel = _rel(root, path)
        if _under(rel, SKIP):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        found.append((machine, rel, [n for n in needed.split(",") if n],
                      [r for r in rpath.replace(";", ":").split(":") if r]))
    return found


def system_machines(root):
    """The CPUs the system's dynamic linkers are for (several: multilib)"""
    from porthole.plugins.crossdev import build
    linkers = build.dynamic_linkers(root)
    if not linkers or not shutil.which("scanelf"):
        return set()
    out = subprocess.run(["scanelf", "-B", "-F", "%a;%F"] + sorted(linkers.values()),
                         capture_output=True, text=True).stdout
    return {line.split(";")[0] for line in out.splitlines() if line.strip()}


def host_machine():
    out = subprocess.run(["scanelf", "-B", "-F", "%a;%F", os.path.realpath("/bin/sh")],
                         capture_output=True, text=True).stdout.strip()
    return out.split(";")[0] if out else None


def library_dirs(root):
    """The folders the system's dynamic linker searches: ld.so.conf (and
    what it includes), env.d's LDPATH (in case env-update hasn't run in it
    yet), and the usual folders"""
    dirs = []

    def read_conf(path, depth=0):
        try:
            with open(path, errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            return
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("include") and depth < 5:
                for pattern in line.split()[1:]:
                    pattern = pattern if pattern.startswith("/") else os.path.join(
                        "/etc", pattern)
                    for match in sorted(glob.glob(root + pattern)):
                        read_conf(match, depth + 1)
            else:
                dirs.extend(d for d in line.replace(",", " ").split() if d.startswith("/"))
    read_conf(os.path.join(root, "etc/ld.so.conf"))
    for path in sorted(glob.glob(os.path.join(root, "etc/env.d/*"))):
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    if line.startswith("LDPATH="):
                        dirs.extend(d for d in line.split("=", 1)[1].strip().strip("'\"")
                                    .split(":") if d.startswith("/"))
        except OSError:
            pass
    dirs += ["/" + d for d in LIB_DIRS]
    unique = []
    for d in dirs:
        if d not in unique:
            unique.append(d)
    return unique


class Libraries:
    """Which library names a folder of the system has (listed once)"""

    def __init__(self, root):
        self.root, self.cache = root, {}

    def has(self, folder, name):
        if folder not in self.cache:
            try:
                self.cache[folder] = set(os.listdir(self.root + folder))
            except OSError:
                self.cache[folder] = set()
        return name in self.cache[folder]


def _expand(rpath, rel_path, lib):
    origin = os.path.dirname(rel_path)
    return os.path.normpath(rpath.replace("$ORIGIN", origin).replace("${ORIGIN}", origin)
                            .replace("$LIB", lib).replace("${LIB}", lib))


def check_programs(root, findings):
    elves = scan_elf(root)
    if elves is None:
        findings.append(Finding(WARNING, "tools", "scanelf (app-misc/pax-utils) isn't "
                                "installed here: programs weren't checked"))
        return
    wanted = system_machines(root)
    host = host_machine()
    leaked, foreign = [], []
    for machine, rel, _needed, _rpath in elves:
        if not wanted or machine in wanted:
            continue
        if machine == host and not _under(rel, FOREIGN_OK):
            leaked.append("%s (%s)" % (rel, machine))
        else:
            foreign.append("%s (%s)" % (rel, machine))
    if leaked:
        findings.append(Finding(
            ERROR, "wrong-cpu", "%d program%s or librar%s built for this computer (%s), not "
            "the system" % (len(leaked), "" if len(leaked) == 1 else "s",
                            "y" if len(leaked) == 1 else "ies", host), leaked,
            "Find the package (Porthole's file search, or qfile -R) and build it again; "
            "it picked up this computer's compiler."))
    if foreign:
        findings.append(Finding(
            WARNING, "foreign-cpu", "%d file%s for another CPU than the system's (%s)"
            % (len(foreign), "" if len(foreign) == 1 else "s", ", ".join(sorted(wanted))),
            foreign, "Usually firmware or build tools; worth a look if any is a program "
            "the system runs."))
    # the libraries they need
    dirs = library_dirs(root)
    libraries = Libraries(root)
    lib = "lib64" if os.path.isdir(os.path.join(root, "usr/lib64")) else "lib"
    missing, bundled = {}, {}
    for machine, rel, needed, rpaths in elves:
        if wanted and machine not in wanted:
            continue
        search = [_expand(r, rel, lib) for r in rpaths] + dirs
        for name in needed:
            if "/" in name:
                found = os.path.lexists(root + name)
            else:
                found = any(libraries.has(d, name) for d in search)
            if found:
                continue
            if libraries.has(os.path.dirname(rel), name):
                continue  # a bundle next to its own libraries (a launcher sets the path)
            target = missing if _under(rel, SYSTEM_DIRS) else bundled
            target.setdefault(name, []).append(rel)
    for found, severity, where in ((missing, ERROR, "the system's"),
                                   (bundled, WARNING, "self-contained (e.g. /opt)")):
        if found:
            items = ["%s, needed by %s%s" % (name, ", ".join(users[:3]),
                                              " and %d more" % (len(users) - 3)
                                              if len(users) > 3 else "")
                     for name, users in sorted(found.items())]
            findings.append(Finding(
                severity, "missing-library", "%d librar%s %s programs need %s missing"
                % (len(found), "y" if len(found) == 1 else "ies", where,
                   "is" if len(found) == 1 else "are"), items,
                "Usually a package that should be rebuilt (emerge @preserved-rebuild) "
                "or a dependency missing from an ebuild."))


# -- module names a cross build gets wrong --------------------------------------------

def check_module_names(root, findings):
    from porthole.plugins.crossdev import build
    moves = build.perl_dir_moves(build.host_perl_dirs(), build.system_perl_dirs(root),
                                 build.PERL_ARCH_DIRS)
    left = [here for here, _theirs in moves
            if os.path.isdir(root + here) and not os.path.islink(root + here)
            and any(files for _d, _s, files in os.walk(root + here))]
    if left:
        findings.append(Finding(
            ERROR, "perl-folder", "Perl modules in this computer's perl folder, where the "
            "system's perl doesn't look", left,
            "Start the build again: its first step moves them (Porthole's repair)."))
    names = build.misnamed_python_modules(root, build.python_suffix_moves(root))
    unlinked = [_rel(root, path) for path, correct in names if not os.path.lexists(correct)]
    if unlinked:
        findings.append(Finding(
            ERROR, "python-name", "%d Python module%s named for this computer's Python"
            % (len(unlinked), "" if len(unlinked) == 1 else "s"), unlinked,
            "Start the build again: its first step links them under the system's names."))


def _binpkg_names(path):
    """Paths inside a binary package (gpkg or xpak tbz2)"""
    import tarfile
    try:
        if path.endswith(".gpkg.tar"):
            with tarfile.open(path) as outer:
                for member in outer.getmembers():
                    if "/image.tar" in member.name:
                        data = outer.extractfile(member).read()
                        if member.name.endswith(".zst"):
                            data = subprocess.run(["zstd", "-dcq"], input=data,
                                                  capture_output=True).stdout
                            mode = "r:"
                        else:
                            mode = "r:*"
                        import io
                        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as image:
                            return image.getnames()
            return []
        with tarfile.open(path, "r:*") as package:
            return package.getnames()
    except (OSError, tarfile.TarError, EOFError):
        return None


def check_binpkgs(target, root, findings, cache_dir=REPORT_DIR):
    """The system's binary packages: none with this computer's perl folders
    or Python endings (checked once per package file)"""
    from porthole.plugins.crossdev import build, conf
    pkgdir = conf.effective(conf.read(target), "PKGDIR")
    if not pkgdir or not os.path.isdir(pkgdir):
        return
    perl = [here.lstrip("/") + "/" for here, _t in build.perl_dir_moves(
        build.host_perl_dirs(), build.system_perl_dirs(root), build.PERL_ARCH_DIRS)]
    python = [mine for mine, _t in build.python_suffix_moves(root)]
    if not perl and not python:
        return
    cache_path = os.path.join(cache_dir, "binpkgs-%s.json" % target.name)
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    signature = "%s|%s" % (",".join(perl), ",".join(python))
    if cache.get("signature") != signature:
        cache = {"signature": signature, "packages": {}}
    bad, checked = [], {}
    for path in sorted(glob.glob(os.path.join(pkgdir, "**", "*.gpkg.tar"), recursive=True) +
                       glob.glob(os.path.join(pkgdir, "**", "*.tbz2"), recursive=True)):
        stat = os.stat(path)
        key = "%d:%d" % (stat.st_size, int(stat.st_mtime))
        known = cache["packages"].get(path)
        if known and known[0] == key:
            wrong = known[1]
        else:
            names = _binpkg_names(path)
            if names is None:
                continue
            wrong = any(any(p in n for p in perl) or any(n.endswith(s) for s in python)
                        for n in names)
        checked[path] = [key, wrong]
        if wrong:
            bad.append(os.path.relpath(path, pkgdir))
    cache["packages"] = checked
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path + ".new", "w") as f:
            json.dump(cache, f)
        os.replace(cache_path + ".new", cache_path)
    except OSError:
        pass
    if bad:
        findings.append(Finding(
            ERROR, "binpkg-names", "%d binary package%s with this computer's perl folders or "
            "Python endings in them" % (len(bad), "" if len(bad) == 1 else "s"), bad,
            "Remove them from %s and build those packages again; installed elsewhere "
            "(another device, an installer) they'd be broken." % pkgdir))


# -- the kernel ---------------------------------------------------------------------

def check_kernel(root, findings):
    no_index = [os.path.basename(d) for d in sorted(glob.glob(os.path.join(root, "lib/modules/*")))
                if os.path.isdir(os.path.join(d, "kernel"))
                and not os.path.exists(os.path.join(d, "modules.dep"))]
    if no_index:
        findings.append(Finding(
            ERROR, "kernel-modules", "Kernel modules without their index (modules.dep)",
            no_index, "Run depmod for that kernel version in the system "
            "(depmod -b ROOT VERSION); the kernel can't load modules without it."))


# -- what portage knows -------------------------------------------------------------

def check_packages(root, findings):
    gone = []
    for contents in sorted(glob.glob(os.path.join(root, "var/db/pkg/*/*/CONTENTS"))):
        package = os.path.relpath(os.path.dirname(contents), os.path.join(root, "var/db/pkg"))
        try:
            with open(contents, errors="replace") as f:
                for line in f:
                    if line.startswith("obj "):
                        path = line[4:].rstrip("\n").rsplit(" ", 2)[0]
                        if not os.path.lexists(root + path):
                            gone.append("%s: %s" % (package, path))
        except OSError:
            pass
    if gone:
        findings.append(Finding(
            WARNING, "files-gone", "%d file%s installed packages list are gone"
            % (len(gone), "" if len(gone) == 1 else "s"), gone,
            "Something removed them after the install; rebuild the package if it matters."))
    registry = os.path.join(root, "var/lib/portage/preserved_libs_registry")
    try:
        with open(registry) as f:
            kept = json.load(f)
    except (OSError, ValueError):
        kept = {}
    if kept:
        findings.append(Finding(
            WARNING, "preserved-libs", "Old libraries kept for programs not rebuilt yet",
            sorted(kept), "Run emerge @preserved-rebuild for the system."))


def check_links(root, findings):
    broken = []
    for top in SYSTEM_DIRS + ("etc",):
        start = os.path.join(root, top)
        if not os.path.isdir(start) or os.path.islink(start):
            continue
        for folder, dirs, files in os.walk(start):
            for name in files + dirs:
                path = os.path.join(folder, name)
                if not os.path.islink(path):
                    continue
                target = os.readlink(path)
                full = root + target if target.startswith("/") else os.path.join(folder, target)
                if target.startswith(("/proc/", "/sys/", "/dev/", "/run/")) \
                        or target in ("/proc", "/sys", "/dev", "/run"):
                    continue  # there on the running system
                if not os.path.exists(full) and not _resolves(root, path):
                    broken.append("%s -> %s" % (_rel(root, path), target))
    if broken:
        findings.append(Finding(
            WARNING, "broken-links", "%d link%s leading nowhere"
            % (len(broken), "" if len(broken) == 1 else "s"), broken))


def _resolves(root, path, depth=0):
    """A link resolved inside root (absolute targets are the system's own)"""
    if depth > 20:
        return False
    if not os.path.islink(path):
        return os.path.exists(path)
    target = os.readlink(path)
    full = root + target if target.startswith("/") else os.path.join(os.path.dirname(path),
                                                                         target)
    parent, base = os.path.split(os.path.normpath(full))
    return _resolves(root, os.path.join(_real_inside(root, parent), base), depth + 1)


def _real_inside(root, folder):
    """folder with the links in it followed inside root"""
    rel = os.path.relpath(folder, root)
    if rel == "." or rel.startswith(".."):
        return root
    current = root
    for part in rel.split(os.sep):
        nxt = os.path.join(current, part)
        hops = 0
        while os.path.islink(nxt) and hops < 20:
            target = os.readlink(nxt)
            nxt = root + target if target.startswith("/") else os.path.normpath(
                os.path.join(current, target))
            hops += 1
        current = nxt
    return current


# -- all of it ------------------------------------------------------------------------

def check(target, log=lambda m: None, binpkgs=True):
    """[Finding] for a system"""
    root = os.path.realpath(target.root)
    findings = []
    for name, step in (("programs", lambda: check_programs(root, findings)),
                       ("module names", lambda: check_module_names(root, findings)),
                       ("binary packages", lambda: check_binpkgs(target, root, findings)
                        if binpkgs else None),
                       ("kernel", lambda: check_kernel(root, findings)),
                       ("packages", lambda: check_packages(root, findings)),
                       ("links", lambda: check_links(root, findings))):
        log("== health: checking %s" % name)
        try:
            step()
        except Exception as e:
            findings.append(Finding(WARNING, "check-failed", "The %s check couldn't run: %s"
                                    % (name, e)))
    return findings


def report_lines(findings):
    lines = []
    for severity, word in ((ERROR, "Error"), (WARNING, "Warning")):
        for finding in [f for f in findings if f.severity == severity]:
            lines.append("%s: %s" % (word, finding.title))
            for item in finding.items[:SHOW]:
                lines.append("    " + item)
            if len(finding.items) > SHOW:
                lines.append("    ... and %d more (full list in the health report)"
                             % (len(finding.items) - SHOW))
            if finding.fix:
                lines.append("    What to do: " + finding.fix)
    return lines


def save_report(target, findings, report_dir=REPORT_DIR):
    import time
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "%s.json" % target.name)
    with open(path + ".new", "w") as f:
        json.dump({"system": target.name, "checked": time.time(),
                   "errors": sum(1 for x in findings if x.severity == ERROR),
                   "warnings": sum(1 for x in findings if x.severity == WARNING),
                   "findings": [x.as_dict() for x in findings]}, f, indent=1)
    os.replace(path + ".new", path)
    return path
