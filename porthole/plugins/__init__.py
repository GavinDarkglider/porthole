"""
    Porthole plugins

    A plugin is a package here, porthole/plugins/<name>/, installed with
    Porthole (separately, or by USE flag from the same tree). It has two
    halves:

      __init__.py  the GUI half: events from the main window ("load",
                   "enable", "unload") to add menu items, views, tabs
      root.py      optional, the root half: commands and job steps that run
                   as root, through porthole-update-service:

                       COMMANDS = {"name": function(args) -> exit status}
                       STEPS = {"name": function(runner, job_id, step, log) -> bool}

                   Each command checks its own arguments, like the service's
                   built-in ones do.

    Root code is only loaded from a plugin that is switched on for the whole
    computer (plugins = ... in /etc/porthole/updater.conf, root's setting)
    and installed where only root can change it: never from a source
    checkout or a user's files.

    Copyright (C) 2003 - 2026 Brian Bockelman, Brian Dolbec, Tommy Iorns,
                              and Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import importlib
import os
import re

PLUGINS_DIR = os.path.dirname(os.path.abspath(__file__))
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class PluginError(Exception):
    pass


def installed():
    """Names of the plugins installed here"""
    try:
        entries = sorted(os.listdir(PLUGINS_DIR))
    except OSError:
        return []
    return [e for e in entries if _NAME.match(e)
            and os.path.isfile(os.path.join(PLUGINS_DIR, e, "__init__.py"))]


def enabled():
    """Plugins switched on for this computer (updater.conf), that are installed"""
    try:
        from porthole.updater.service import load_config
        wanted = load_config().get("plugins") or []
    except Exception:
        return []
    have = installed()
    return [name for name in wanted if name in have]


def safe_location(path):
    """Whether only root can change path and every folder above it"""
    path = os.path.realpath(path)
    while True:
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_uid != 0 or st.st_mode & 0o022:
            return False
        parent = os.path.dirname(path)
        if parent == path:
            return True
        path = parent


def describe(name):
    """(title, description) of an installed plugin, from TITLE and
    DESCRIPTION in its __init__.py, read without running it (a plugin that
    isn't switched on is never imported)"""
    import ast
    path = os.path.join(PLUGINS_DIR, name, "__init__.py")
    found = {"TITLE": name, "DESCRIPTION": ""}
    try:
        with open(path) as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return found["TITLE"], found["DESCRIPTION"]
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                getattr(node.targets[0], "id", None) in found:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(value, str):
                found[node.targets[0].id] = " ".join(value.split())
    return found["TITLE"], found["DESCRIPTION"]


def root_module(name):
    """The root half of a plugin, for porthole-update-service and the
    runner: only an enabled plugin, installed where only root can change
    it. PluginError otherwise."""
    if not _NAME.match(name or ""):
        raise PluginError("Not a plugin name: %r" % name)
    if name not in installed():
        raise PluginError("No plugin called %s is installed" % name)
    if name not in enabled():
        raise PluginError("The %s plugin isn't switched on (Settings, Plugins)" % name)
    path = os.path.join(PLUGINS_DIR, name, "root.py")
    if not os.path.isfile(path):
        raise PluginError("The %s plugin has nothing that runs as root" % name)
    if os.geteuid() == 0 and not safe_location(path):
        raise PluginError("Not loading %s as root: it is in a place users can change (%s)"
                          % (name, path))
    return importlib.import_module("porthole.plugins.%s.root" % name)
