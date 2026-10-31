"""
    Porthole crossdev plugin (root half)

    Commands run through porthole-update-service as root (only while the
    plugin is switched on and installed where only root can change it):

        plugin crossdev check [ARCH [TUPLE]]   the readiness checks, as JSON
        plugin crossdev make-repo              create the local repository
                                               crossdev writes its toolchain
                                               ebuilds into

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import os
import re
import sys

from porthole.plugins.crossdev import checks

_ARCH = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_TUPLE = re.compile(r"^[a-z0-9_]+(-[a-z0-9_.]+){1,4}$")


def check(args):
    arch = args[0] if args else None
    tuple_ = args[1] if len(args) > 1 else None
    if (arch and not _ARCH.match(arch)) or (tuple_ and not _TUPLE.match(tuple_)) or len(args) > 2:
        print("usage: plugin crossdev check [ARCH [TUPLE]]", file=sys.stderr)
        return 2
    print(json.dumps([vars(c) for c in checks.check_all(arch, tuple_)], indent=1))
    return 0


def make_repo(args, portage_dir="/etc/portage", location=checks.REPO_DIR):
    """The repository crossdev needs, the way the Gentoo wiki sets it up:
    local, never synced, using the gentoo repository's eclasses"""
    from porthole.updater import system
    if args:
        print("usage: plugin crossdev make-repo", file=sys.stderr)
        return 2
    name = checks.REPO_NAME
    if name in system.repos_conf_sections(portage_dir):
        print("There already is a repository called %s" % name, file=sys.stderr)
        return 1
    directory = os.path.join(portage_dir, "repos.conf")
    if os.path.isfile(directory):
        print("%s is a single file. Porthole adds each repository in a file of its "
              "own: move it into a new directory of that name first." % directory,
              file=sys.stderr)
        return 1
    repo_name = os.path.join(location, "profiles", "repo_name")
    if os.path.exists(location):
        # left from before: used again only if it is this repository
        try:
            with open(repo_name) as f:
                mine = f.read().strip() == name
        except OSError:
            mine = False
        if not mine:
            print("%s is there already and isn't a %s repository; move it away first"
                  % (location, name), file=sys.stderr)
            return 1
    else:
        os.makedirs(os.path.join(location, "profiles"), mode=0o755)
        os.makedirs(os.path.join(location, "metadata"), mode=0o755)
        with open(repo_name, "w") as f:
            f.write(name + "\n")
        with open(os.path.join(location, "metadata", "layout.conf"), "w") as f:
            f.write("masters = gentoo\nthin-manifests = true\n")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name + ".conf")
    with open(path + ".new", "w") as f:
        f.write("# Added in Porthole (crossdev plugin): crossdev's toolchain packages\n"
                "[%s]\nlocation = %s\npriority = 10\nmasters = gentoo\nauto-sync = no\n"
                % (name, location))
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    print("Created %s (%s)" % (name, location))
    return 0


COMMANDS = {"check": check, "make-repo": make_repo}
STEPS = {}
