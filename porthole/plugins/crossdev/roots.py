"""
    Porthole crossdev plugin: the systems built here for other computers

    Each is a root of its own, /var/lib/porthole/roots/<name>, which is
    also its config root (its own etc/portage) and a target in
    /etc/porthole/targets.conf, so plans, fixes and jobs work on it the way
    they do on this computer.

    Its repositories are this computer's, at the same paths: they are
    synced once, here, and a chroot into the root later bind-mounts them
    where its config expects them. For the same reason make.profile is an
    absolute link. make.conf holds no CBUILD: cross builds get it from
    their job (as crossdev's cross-emerge does), so the same make.conf
    works in the emulator, where the root builds for itself.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import configparser
import os
import shutil

from porthole.plugins.crossdev import checks
from porthole.updater import targets

ROOTS_DIR = checks.ROOTS_DIR
BINPKGS_DIR = "/var/cache/porthole/binpkgs"
DEFAULT_REPOS = "/usr/share/portage/config/repos.conf"
GENTOO_KEY = "/usr/share/openpgp-keys/gentoo-release.asc"
MADE_BY = "crossdev"


class RootError(Exception):
    pass


def _paths(roots_dir=None, targets_file=None, binpkgs_dir=None):
    return (roots_dir or ROOTS_DIR, targets_file or targets.TARGETS_FILE,
            binpkgs_dir or BINPKGS_DIR)


def host_repos(portage_dir="/etc/portage", defaults=DEFAULT_REPOS):
    """{name: location} of this computer's repositories, read afresh:
    repos.conf, and gentoo from portage's defaults if repos.conf doesn't
    define it"""
    found = checks.repos(portage_dir)
    if "gentoo" not in found:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(defaults)
            found["gentoo"] = parser.get("gentoo", "location").strip()
        except configparser.Error:
            pass
    return {name: location.rstrip("/") or "/" for name, location in found.items()
            if location}


def host_arch():
    try:
        import portage
        return portage.settings["ARCH"]
    except Exception:
        return None


def foreign_profiles(repos, arch=None):
    """[(repo, arch, profile, status)] from the repositories' profiles.desc,
    for architectures other than this computer's (arch), stable first"""
    found = []
    for repo, location in sorted(repos.items()):
        try:
            with open(os.path.join(location, "profiles", "profiles.desc")) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            words = line.split("#", 1)[0].split()
            if len(words) >= 3 and words[0] != arch:
                found.append((repo, words[0], words[1], words[2]))
    order = {"stable": 0, "dev": 1, "exp": 2}

    def newest_first(profile):
        # 23.0 before 17.0; a profile before the ones under it
        parts = []
        for part in profile.split("/"):
            try:
                parts.append((0, -float(part), ""))
            except ValueError:
                parts.append((1, 0, part))
        return parts
    return sorted(found, key=lambda p: (p[0] == "gentoo", p[0], p[1],
                                        order.get(p[3], 3), newest_first(p[2])))


def made_here(roots_dir=None, targets_file=None):
    """{name: Target} of the systems this plugin made"""
    roots_dir, targets_file, _ = _paths(roots_dir, targets_file)
    roots_dir = os.path.realpath(roots_dir)
    return {name: t for name, t in targets.load(targets_file).items()
            if not t.local and os.path.dirname(os.path.realpath(t.root)) == roots_dir}


def _skeleton(root, split_usr):
    """The top folders, the merged /usr links (unless the profile is a
    split-usr one), and the places repositories and downloads appear in a
    chroot"""
    for d in ("boot", "dev", "etc", "home", "mnt", "proc", "run", "sys", "usr/bin",
              "usr/lib", "usr/lib64", "var/cache/distfiles", "var/db/repos"):
        os.makedirs(os.path.join(root, d), mode=0o755, exist_ok=True)
    os.makedirs(os.path.join(root, "root"), mode=0o700, exist_ok=True)
    for d in ("tmp", "var/tmp"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
        os.chmod(os.path.join(root, d), 0o1777)
    if split_usr:
        for d in ("bin", "sbin", "lib", "lib64", "usr/sbin"):
            os.makedirs(os.path.join(root, d), mode=0o755, exist_ok=True)
    else:
        for link, to in (("bin", "usr/bin"), ("sbin", "usr/bin"), ("usr/sbin", "bin"),
                         ("lib", "usr/lib"), ("lib64", "usr/lib64")):
            os.symlink(to, os.path.join(root, link))


def create(name, title, repo, profile, extra_repos=(), roots_dir=None,
           targets_file=None, repos=None, arch=None,
           binpkgs_dir=None, gentoo_key=None):
    """Make a new system: its root and config, and its entry in
    targets.conf. Returns its Target."""
    roots_dir, targets_file, binpkgs_dir = _paths(roots_dir, targets_file, binpkgs_dir)
    gentoo_key = gentoo_key or GENTOO_KEY
    title = " ".join((title or "").split())[:80] or name
    if not targets._NAME.match(name or "") or name == targets.LOCAL:
        raise RootError("A name is lower case letters, digits, - and _ (up to 41), "
                        "starting with a letter or digit")
    existing = targets.load(targets_file)
    if name in existing:
        raise RootError("There already is a system called %s" % name)
    root = os.path.join(roots_dir, name)
    if os.path.lexists(root):
        raise RootError("%s is there already; move it away first" % root)
    repos = host_repos() if repos is None else repos
    profiles = foreign_profiles(repos, host_arch() if arch is None else arch)
    match = [p for p in profiles if p[0] == repo and p[2] == profile]
    if not match:
        raise RootError("Not a profile for another architecture: %s:%s" % (repo, profile))
    wanted = ["gentoo", repo] + [r for r in extra_repos if r not in ("gentoo", repo)]
    unknown = [r for r in wanted if r not in repos]
    if unknown:
        raise RootError("Not a repository on this computer: %s" % " ".join(unknown))
    profile_dir = os.path.join(repos[repo], "profiles", profile)
    if not os.path.isdir(profile_dir):
        raise RootError("No such profile directory: %s" % profile_dir)
    if os.path.isfile(targets_file + ".new"):
        os.unlink(targets_file + ".new")

    os.makedirs(roots_dir, mode=0o755, exist_ok=True)
    os.mkdir(root, 0o755)
    try:
        _skeleton(root, "split-usr" in profile.split("/"))
        for r in wanted:
            os.makedirs(os.path.join(root, repos[r].lstrip("/")), mode=0o755, exist_ok=True)
        portage_dir = os.path.join(root, "etc", "portage")
        os.makedirs(os.path.join(portage_dir, "repos.conf"), mode=0o755)
        with open(os.path.join(portage_dir, "repos.conf", "porthole.conf"), "w") as f:
            f.write("# Made by Porthole: this computer's repositories, at the same paths "
                    "(synced there,\n# not here). Ticked in Porthole for %s.\n" % title)
            for r in wanted:
                f.write("\n[%s]\nlocation = %s\nauto-sync = no\n" % (r, repos[r]))
        os.symlink(profile_dir, os.path.join(portage_dir, "make.profile"))
        with open(os.path.join(portage_dir, "make.conf"), "w") as f:
            f.write(
                "# Made by Porthole for %s (%s), profile %s:%s\n"
                "#\n"
                "# CHOST, CFLAGS and USE come from the profile. CBUILD isn't set here:\n"
                "# cross builds on the computer that built this system set it\n"
                "# themselves, and in the emulator the system builds for itself.\n"
                "\n"
                "# packages built for this system, kept apart from that computer's own\n"
                'PKGDIR="%s"\n'
                "# downloads are shared with that computer\n"
                'DISTDIR="/var/cache/distfiles"\n'
                "\n"
                "# Your own settings below.\n"
                % (title, name, repo, profile, os.path.join(binpkgs_dir, name)))
        if os.path.isfile(gentoo_key):
            keys = os.path.join(root, "usr", "share", "openpgp-keys")
            os.makedirs(keys, mode=0o755, exist_ok=True)
            shutil.copy2(gentoo_key, keys)
        # last: only a complete system becomes a target
        text = ""
        if os.path.exists(targets_file):
            with open(targets_file) as f:
                text = f.read()
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n[%s]\ntitle = %s\nconfig_root = %s\nroot = %s\nmade_by = %s\n"
                 % (name, title.replace("%", "%%"), root, root, MADE_BY))
        os.makedirs(os.path.dirname(targets_file), mode=0o755, exist_ok=True)
        with open(targets_file + ".new", "w") as f:
            f.write(text)
        os.chmod(targets_file + ".new", 0o644)
        os.replace(targets_file + ".new", targets_file)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return targets.load(targets_file)[name]


def mounts_under(path, mounts_file="/proc/self/mounts"):
    """Mount points at or under path (a chroot's bind mounts)"""
    path = os.path.realpath(path).rstrip("/") + "/"
    found = []
    try:
        with open(mounts_file) as f:
            for line in f:
                point = line.split()[1].encode().decode("unicode_escape")
                if (point.rstrip("/") + "/").startswith(path):
                    found.append(point)
    except OSError:
        pass
    return found


def check_removable(name, roots_dir=None, targets_file=None):
    """The Target of a system this plugin made, if it can be removed now"""
    roots_dir, targets_file, _ = _paths(roots_dir, targets_file)
    target = made_here(roots_dir, targets_file).get(name)
    if target is None:
        raise RootError("No system called %s was made in %s" % (name, roots_dir))
    busy = mounts_under(target.root)
    if busy:
        raise RootError("%s still has things mounted in it (%s); a build may be "
                        "using it" % (name, ", ".join(busy[:3])))
    return target


def remove(name, roots_dir=None, targets_file=None,
           binpkgs_dir=None, log=lambda m: None):
    """Delete a system: its targets.conf entry first (so nothing plans for
    it any more), then its root and its packages"""
    roots_dir, targets_file, binpkgs_dir = _paths(roots_dir, targets_file, binpkgs_dir)
    target = check_removable(name, roots_dir, targets_file)
    lines, skip = [], False
    with open(targets_file) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                skip = stripped[1:-1].strip() == name
            if not skip:
                lines.append(line)
    with open(targets_file + ".new", "w") as f:
        f.write("".join(lines))
    os.chmod(targets_file + ".new", 0o644)
    os.replace(targets_file + ".new", targets_file)
    log("Removed %s from %s" % (name, targets_file))
    # one more look: nothing may have been mounted in the meantime
    if mounts_under(target.root):
        raise RootError("Something was mounted in %s; not deleting it" % target.root)
    shutil.rmtree(target.root)
    log("Deleted %s" % target.root)
    packages = os.path.join(binpkgs_dir, name)
    if os.path.isdir(packages):
        shutil.rmtree(packages)
        log("Deleted %s" % packages)
    return True
