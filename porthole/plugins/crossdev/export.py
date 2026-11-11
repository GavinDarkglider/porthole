"""
    Porthole crossdev plugin: a built system copied out, for its computer

    The root itself stays set up for building here: its repositories are
    this computer's, at this computer's paths, and make.profile links into
    them. The copy gets settings of its own: each repository it uses other
    than gentoo copied in (a snapshot, never synced: the computer it runs
    on has no other way to get local changes), at /var/db/repos/<name>;
    repos.conf and make.profile pointing there; gentoo left to portage's
    own default (/var/db/repos/gentoo, synced on that computer: emerge
    --sync or emerge-webrsync before its first emerge). What only matters
    to building here goes: the folders the repositories were mounted on,
    the binary package folder and setting, the cross build hook.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import shutil
import subprocess

# marks a folder as a copy made here, so copying again may replace it
MARK = "etc/porthole-export"
REPOS_DIR = "/var/db/repos"


class ExportError(Exception):
    pass


def _inside(base, path):
    return os.path.join(base, path.lstrip("/"))


def _is_under(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def device_location(name):
    return os.path.join(REPOS_DIR, name)


def profile_of(target, repos):
    """(repo, profile) make.profile points at, from the repositories'
    locations here, or (None, None)"""
    link = os.path.realpath(os.path.join(target.portage_dir, "make.profile"))
    for name, location in repos.items():
        prefix = os.path.realpath(os.path.join(location, "profiles")) + "/"
        if link.startswith(prefix):
            return name, link[len(prefix):]
    return None, None


def check_dest(root, dest):
    """dest, if the system can be copied there: an existing folder, empty
    or an earlier copy of it, not inside the root or the other way round"""
    dest = os.path.realpath(dest)
    if not os.path.isdir(dest):
        raise ExportError("%s isn't a folder" % dest)
    if _is_under(dest, root) or _is_under(root, dest):
        raise ExportError("%s and the system's folder %s overlap" % (dest, root))
    entries = [e for e in os.listdir(dest) if e != "lost+found"]
    if entries and not os.path.exists(_inside(dest, MARK)):
        raise ExportError("%s isn't empty, and isn't an earlier copy made by Porthole: "
                          "nothing was copied" % dest)
    return dest, bool(entries)


def _drop_empty_parents(dest, path):
    """path in the copy, then each folder above it while empty, top-level
    folders (home, usr ...) kept"""
    path = _inside(dest, path)
    while os.path.dirname(path.rstrip("/")) != dest.rstrip("/"):
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass  # not copied at all
        except OSError:
            return
        path = os.path.dirname(path.rstrip("/"))


def _strip_block(path, begin, end):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return
    if begin in text and end in text:
        before, rest = text.split(begin, 1)
        after = rest.split(end, 1)[1]
        before, after = before.rstrip("\n"), after.lstrip("\n")
        text = "\n\n".join(t for t in (before, after) if t.strip())
        text += "\n" if text else ""
        with open(path, "w") as f:
            f.write(text)


def _device_make_conf(path):
    """make.conf without what is about building here: the binary packages
    kept for this computer (folder and FEATURES=buildpkg block), and the
    note that downloads are shared with it"""
    from porthole.plugins.crossdev import roots
    try:
        with open(path) as f:
            lines = f.read().splitlines(True)
    except OSError:
        return
    out = []
    for line in lines:
        if line.startswith("# downloads are shared with that computer"):
            continue
        if line.startswith("PKGDIR="):
            if out and out[-1].startswith("# packages built for this system"):
                out.pop()
            continue
        out.append(line)
    with open(path, "w") as f:
        f.write("".join(out))
    _strip_block(path, roots.BINPKG_BEGIN, roots.BINPKG_END)


def export(target, dest, repos, log=print):
    """Copy the system in target.root to dest, with settings for the
    computer it runs on (see the module's notes)"""
    from porthole.plugins.crossdev import build
    root = os.path.realpath(target.root)
    dest, again = check_dest(root, dest)
    repo, profile = profile_of(target, repos)
    if repo is None:
        raise ExportError("%s's make.profile doesn't point into one of its repositories"
                          % target.title)
    # the folders the repositories are mounted on while building: not copied
    mirrors = [loc for name, loc in repos.items() if loc != device_location(name)]
    argv = ["rsync", "-aHAX", "--numeric-ids", "--info=progress2"]
    if again:
        argv.append("--delete")
    argv += ["--exclude=/" + MARK]
    argv += ["--exclude=%s/" % loc.rstrip("/") for loc in mirrors]
    argv += [root + "/", dest + "/"]
    log("$ " + " ".join(argv))
    if subprocess.call(argv) != 0:
        raise ExportError("rsync didn't copy everything (above)")
    for location in mirrors:
        _drop_empty_parents(dest, location)
    # the repositories it uses, gentoo apart
    portage_dir = _inside(dest, "etc/portage")
    lines = ["# Made by Porthole when %s was copied out of the computer that built it." %
             target.title,
             "# gentoo is portage's own default (/var/db/repos/gentoo): run emerge --sync",
             "# or emerge-webrsync before the first emerge. The others are copies of",
             "# what it was built from, never synced: they hold changes made there."]
    for name, location in repos.items():
        if name == "gentoo":
            continue
        there = device_location(name)
        copy = _inside(dest, there)
        log("== %s: %s copied to %s" % (name, location, there))
        if os.path.lexists(copy):
            shutil.rmtree(copy)
        shutil.copytree(location, copy, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", ".svn", "distfiles", "packages"))
        lines += ["", "[%s]" % name, "location = %s" % there, "auto-sync = no"]
    conf_dir = os.path.join(portage_dir, "repos.conf")
    if os.path.isdir(conf_dir):
        shutil.rmtree(conf_dir)
    elif os.path.lexists(conf_dir):
        os.unlink(conf_dir)
    os.makedirs(conf_dir, mode=0o755)
    with open(os.path.join(conf_dir, "porthole.conf"), "w") as f:
        f.write("\n".join(lines) + "\n")
    link = os.path.join(portage_dir, "make.profile")
    os.unlink(link)
    os.symlink(os.path.join(device_location(repo), "profiles", profile), link)
    log("== make.profile: %s" % os.readlink(link))
    _device_make_conf(os.path.join(portage_dir, "make.conf"))
    _strip_block(os.path.join(portage_dir, "bashrc"), build.BASHRC_BEGIN, build.BASHRC_END)
    bashrc = os.path.join(portage_dir, "bashrc")
    if os.path.isfile(bashrc) and not open(bashrc).read().strip():
        os.unlink(bashrc)
    with open(_inside(dest, MARK), "w") as f:
        f.write("Copied by Porthole from %s (%s); copying again replaces it.\n"
                % (root, target.title))
    return dest
