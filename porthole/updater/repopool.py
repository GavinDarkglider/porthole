"""
    Porthole: repositories for the other systems only

    A board's repository (like the Nintendo Switch overlay) changes
    packages for that board; this computer must not use it. Porthole keeps
    such repositories in a list of its own, /etc/porthole/repos.conf
    (repos.conf format, root's to change), never in this computer's
    /etc/portage/repos.conf. They are checked out here (in /var/db/repos,
    or a folder of the user's that Porthole doesn't sync) and synced by
    Porthole, with emaint given only this list (PORTAGE_REPOSITORIES), so
    this computer's own repositories aren't touched. The systems that use
    one list it in their own repos.conf, at the same path.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import configparser
import os

POOL_FILE = "/etc/porthole/repos.conf"
HEADER = ("# Porthole: repositories for the systems Porthole builds for other computers,\n"
          "# not used by this computer. Changed in Porthole (Settings, Repositories).\n")


def _parser(path=None):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path or POOL_FILE)
    except configparser.Error:
        pass
    return parser


def repos(path=None):
    """{name: {"location", "sync_type", "sync_uri", "auto_sync"}}"""
    parser = _parser(path)
    found = {}
    for name in parser.sections():
        section = parser[name]
        location = section.get("location", "").strip()
        if not location:
            continue
        found[name] = {"location": location.rstrip("/") or "/",
                       "sync_type": section.get("sync-type", "").strip(),
                       "sync_uri": section.get("sync-uri", "").strip(),
                       "auto_sync": section.get("auto-sync", "yes").strip().lower()
                       in ("yes", "true", "1"),
                       # used from a folder of the user's for now (see use_folder)
                       "synced": {k: section.get("porthole-synced-" + k, "").strip()
                                  for k in SYNCED_KEYS}
                       if section.get("porthole-synced-location") else None}
    return found


def _write(parser, path):
    path = path or POOL_FILE
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path + ".new", "w") as f:
        f.write(HEADER)
        for name in parser.sections():
            f.write("\n[%s]\n" % name)
            for key, value in parser[name].items():
                f.write("%s = %s\n" % (key, value))
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)


def add(name, location, sync_type="", sync_uri="", auto_sync=True, path=None):
    parser = _parser(path)
    if parser.has_section(name):
        raise ValueError("There already is a repository called %s" % name)
    parser.add_section(name)
    parser[name]["location"] = location
    if sync_type:
        parser[name]["sync-type"] = sync_type
        parser[name]["sync-uri"] = sync_uri
        parser[name]["auto-sync"] = "yes" if auto_sync else "no"
    _write(parser, path)


SYNCED_KEYS = ("location", "sync-type", "sync-uri", "auto-sync")


def use_folder(name, folder, path=None):
    """Use a folder of the user's (a working copy, say a fix not pushed
    yet) in place of a repository's synced checkout, without syncing it;
    folder None goes back to the synced checkout. The synced checkout's
    settings are kept in the entry (porthole-synced-*) meanwhile."""
    parser = _parser(path)
    if not parser.has_section(name):
        raise ValueError("%s isn't one of the repositories for other systems" % name)
    section = parser[name]
    if folder:
        if not section.get("porthole-synced-location"):
            for key in SYNCED_KEYS:
                if section.get(key):
                    section["porthole-synced-" + key] = section[key]
        for key in ("sync-type", "sync-uri", "auto-sync"):
            section.pop(key, None)
        section["location"] = folder
        section["auto-sync"] = "no"
    else:
        if not section.get("porthole-synced-location"):
            raise ValueError("%s isn't used from a folder" % name)
        for key in SYNCED_KEYS:
            section.pop(key, None)
            saved = section.pop("porthole-synced-" + key, None)
            if saved:
                section[key] = saved
    _write(parser, path)
    return section["location"]


def remove(name, path=None):
    parser = _parser(path)
    if not parser.remove_section(name):
        raise ValueError("%s isn't one of the repositories for other systems" % name)
    _write(parser, path)


def environment(names=None, path=None, gentoo="/var/db/repos/gentoo"):
    """PORTAGE_REPOSITORIES for emaint: the list's repositories (only
    names, if given), with gentoo as the main repository, not synced"""
    lines = ["[DEFAULT]", "main-repo = gentoo", "", "[gentoo]",
             "location = %s" % gentoo, "auto-sync = no"]
    for name, repo in repos(path).items():
        if names is not None and name not in names:
            continue
        lines += ["", "[%s]" % name, "location = %s" % repo["location"]]
        if repo["sync_type"]:
            lines += ["sync-type = %s" % repo["sync_type"], "sync-uri = %s" % repo["sync_uri"],
                      "auto-sync = %s" % ("yes" if repo["auto_sync"] else "no")]
        else:
            lines.append("auto-sync = no")
    return "\n".join(lines) + "\n"


# -- the repositories a system uses (its etc/portage/repos.conf/porthole.conf) --

TARGET_FILE = "porthole.conf"


def target_file(portage_dir):
    return os.path.join(portage_dir, "repos.conf", TARGET_FILE)


def target_repos(portage_dir):
    """[name] of the repositories Porthole set up for a system, in order"""
    return _parser(target_file(portage_dir)).sections()


def write_target_repos(portage_dir, locations, title=""):
    """Set the repositories a system uses: {name: location} (ordered),
    gentoo always. They are synced on this computer, not by the system."""
    path = target_file(portage_dir)
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path + ".new", "w") as f:
        f.write("# Made by Porthole: repositories on the computer that builds this system,\n"
                "# at the same paths (synced there, not here)%s.\n"
                % (" for %s" % title if title else ""))
        for name, location in locations.items():
            f.write("\n[%s]\nlocation = %s\nauto-sync = no\n" % (name, location))
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
