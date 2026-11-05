#!/usr/bin/env python3

'''
    Porthole: offers to switch to a new profile or compiler

    Neither happens by itself in Gentoo: portage installs a new GCC beside
    the old one but keeps using the selected one (gcc-config), and a new
    profile version (or one replacing a deprecated profile) is only
    selected with eselect profile. Both are offered in the update review,
    unticked; switching always ends with rebuilding everything
    (emerge --emptytree @world), so all of the system is built the new way.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
'''

import os
import re
import subprocess

GCC = "sys-devel/gcc"
LIBCS = ("sys-libs/glibc", "sys-libs/musl")
EMERGE_LOG = "/var/log/emerge.log"
_VERSION = re.compile(r"^\d+(\.\d+)*$")
_STARTED = re.compile(r"^(\d+):\s+>>> emerge \(\d+ of \d+\) (\S+) to /\s*$")
_FINISHED = re.compile(r"^(\d+):\s+::: completed emerge \(\d+ of \d+\) (\S+) to /\s*$")
# a binary package's merge, logged like a build: not a build time
_BINARY = re.compile(r"^\d+:\s+=== \(\d+ of \d+\) Merging Binary \((\S+?)::")


def _major(version):
    head = version.split(".")[0]
    return int(head) if head.isdigit() else None


# -- how long a full rebuild takes ---------------------------------------------------

def build_times(log_path=EMERGE_LOG):
    """{cp: seconds} the last build of each package took, from portage's
    own emerge.log (binary packages left out: they weren't built here)"""
    from portage.versions import cpv_getkey
    started, took, binary = {}, {}, set()
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                match = _STARTED.match(line)
                if match:
                    started[match.group(2)] = int(match.group(1))
                    continue
                match = _BINARY.match(line)
                if match:
                    binary.add(match.group(1))
                    continue
                match = _FINISHED.match(line)
                if match and match.group(2) in started:
                    cpv = match.group(2)
                    if cpv in binary:
                        binary.discard(cpv)
                        started.pop(cpv)
                        continue
                    try:
                        took[cpv_getkey(cpv)] = int(match.group(1)) - started.pop(cpv)
                    except Exception:
                        pass
    except OSError:
        pass
    return took


def rebuild_estimate(vardb=None, log_path=EMERGE_LOG):
    """{"packages": installed count, "seconds": the sum of their last
    builds, "unknown": how many have no build in the log (binary packages,
    or the log was cleaned)}"""
    if vardb is None:
        import portage
        vardb = portage.db[portage.root]["vartree"].dbapi
    times = build_times(log_path)
    cps = vardb.cp_all()
    known = [times[cp] for cp in cps if cp in times]
    return {"packages": len(cps), "seconds": sum(known), "unknown": len(cps) - len(known)}


# -- a new GCC -----------------------------------------------------------------------

def gcc_offer(current, chost, merges, vardb=None, portdb=None):
    """{"major", "current_major", "switch_to", "install"} when a newer GCC
    major than the selected one is installed, comes with this update, or
    can be installed; else None. current: gcc-config -c."""
    if not current or not chost:
        return None
    current_major = _major(current.rsplit("-", 1)[-1])
    if current_major is None:
        return None
    if vardb is None or portdb is None:
        import portage
        vardb = vardb or portage.db[portage.root]["vartree"].dbapi
        portdb = portdb or portage.db[portage.root]["porttree"].dbapi
    from portage.versions import cpv_getversion
    candidates = {}  # major: (cpv, installed or coming)
    for cpv in vardb.match(GCC):
        candidates.setdefault(_major(cpv_getversion(cpv)), (cpv, True))
    for m in merges:
        if m["cp"] == GCC:
            candidates[_major(m["version"])] = (m["cpv"], True)
    best = portdb.xmatch("bestmatch-visible", GCC)
    if best:
        candidates.setdefault(_major(cpv_getversion(best)), (best, False))
    newer = [m for m in candidates if m is not None and m > current_major]
    if not newer:
        return None
    major = max(newer)
    cpv, have = candidates[major]
    return {"major": major, "current_major": current_major,
            "switch_to": "%s-%d" % (chost, major), "install": None if have else cpv}


# -- a new profile -------------------------------------------------------------------

def _repo_of(directory):
    import portage
    for repo in portage.settings.repositories:
        if os.path.join(repo.location, "profiles") == directory:
            return repo.name
    return None


def _newer_name(name, other):
    """other's version when it is name with one version part higher
    (default/linux/amd64/23.0/desktop -> .../25.0/desktop), else None"""
    from portage.versions import vercmp
    mine, theirs = name.split("/"), other.split("/")
    if len(mine) != len(theirs):
        return None
    differ = [i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b]
    if len(differ) != 1:
        return None
    a, b = mine[differ[0]], theirs[differ[0]]
    if _VERSION.match(a) and _VERSION.match(b) and vercmp(b, a) > 0:
        return b
    return None


def profile_offer(config_root="/"):
    """{"from", "to", "repo", "eselect", "deprecated", "instructions"} when
    the profile is deprecated (its deprecated file names the replacement)
    or a newer version of it is listed; else None"""
    from portage.versions import vercmp
    from porthole.updater import system
    name, path = system.profile(config_root)
    if not name or not path:
        return None
    listed = system.profiles()
    mine = next((p for p in listed if p[0] == name), None)
    to, instructions, deprecated = None, "", False
    try:
        with open(os.path.join(path, "deprecated"), errors="replace") as f:
            lines = f.read().splitlines()
        if lines and lines[0].strip():
            to, deprecated = lines[0].strip(), True
            instructions = "\n".join(lines[1:]).strip()
    except OSError:
        pass
    where = mine[2] if mine else None
    if to is None and mine:
        best = None
        order = {"stable": 0, "dev": 1, "exp": 2}
        for other, status, directory in listed:
            if directory != mine[2] or order.get(status, 3) > order.get(mine[1], 3):
                continue
            version = _newer_name(name, other)
            if version and (best is None or vercmp(version, best[0]) > 0):
                best = (version, other)
        if best:
            to = best[1]
    if to is None:
        return None
    match = next((p for p in listed if p[0] == to), None)
    if match:
        where = match[2]
    elif path.endswith(os.sep + name):
        # the replacement is named relative to the same profiles folder
        where = path[:-len(name)].rstrip(os.sep)
    if not where or not os.path.isdir(os.path.join(where, to)):
        return None
    repo = _repo_of(where) or "gentoo"
    return {"from": name, "to": to, "repo": repo, "deprecated": deprecated,
            "eselect": to if repo == "gentoo" else "%s:%s" % (repo, to),
            "instructions": instructions, "profiles_dir": where}


# -- the stages ----------------------------------------------------------------------

def offer_key(kind, offer):
    return "switch-gcc:%d" % offer["major"] if kind == "gcc" else "switch-profile:" + offer["to"]


def libc_installed(vardb=None):
    if vardb is None:
        import portage
        vardb = portage.db[portage.root]["vartree"].dbapi
    return [cp for cp in LIBCS if vardb.match(cp)]


def stages(profile, gcc, options, libcs):
    """The stages of the switches chosen (either may be None): the switch
    itself, the toolchain built again, then everything built again"""
    out = []
    keys = []
    if profile:
        keys.append(offer_key("profile", profile))
        out.append({
            "key": keys[-1], "approval": True,
            "title": "Switch the profile to %s" % profile["to"],
            "why": "eselect profile set selects the new profile; the toolchain and then "
                   "every package are built again under it.",
            "commands": [["eselect", "profile", "set", profile["eselect"]], ["env-update"]],
            "verified": "after previous stage"})
    if gcc:
        keys.append(offer_key("gcc", gcc))
        commands = []
        if gcc.get("install"):
            commands.append(["emerge", "--oneshot"] + options + ["=" + gcc["install"]])
        commands += [["gcc-config", gcc["switch_to"]], ["env-update"],
                     ["emerge", "--oneshot", "dev-build/libtool"]]
        out.append({
            "key": keys[-1], "approval": True,
            "title": "Switch from GCC %d to GCC %d" % (gcc["current_major"], gcc["major"]),
            "why": "gcc-config selects the new compiler; everything after this is built "
                   "with it.",
            "commands": commands, "verified": "after previous stage"})
    if profile:
        # the toolchain first, under the new profile (and with the new GCC)
        out.append({
            "key": keys[0], "approval": True,
            "title": "Build the toolchain again under the new profile",
            "why": "binutils, GCC and the C library first, so everything after is built "
                   "by a toolchain made for the new profile.",
            "commands": [["emerge", "--oneshot", "--usepkg=n", "--getbinpkg=n",
                          "sys-devel/binutils", "sys-devel/gcc"] + libcs
                         + ["dev-build/libtool"]],
            "verified": "after previous stage"})
    if out:
        out.append({
            "key": keys[-1], "approval": True,
            "title": "Build everything again (emerge --emptytree @world)",
            "why": "Every installed package is built again with the new %s, never from "
                   "binary packages made the old way." % " and ".join(
                       (["profile"] if profile else []) + (["compiler"] if gcc else [])),
            "commands": [["emerge", "--emptytree", "--keep-going"] + options
                         + ["--usepkg=n", "--getbinpkg=n", "@world"]],
            "verified": "after previous stage"})
    return out


def eselect_profile_ok(target):
    """True when eselect knows the profile name (checked before offering)"""
    try:
        out = subprocess.run(["eselect", "--brief", "profile", "list"],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # eselect can't be asked; its own error shows when it runs
    names = {line.split()[0] for line in out.splitlines() if line.strip()}
    return not names or target in names
