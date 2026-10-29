"""
    Porthole update assistant: locating porthole's own programs

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import shlex
import shutil
import sys

SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# running from a source checkout rather than an installed copy
LOCAL = os.path.isfile(os.path.join(SRC, "scripts", "porthole")) and not SRC.startswith("/usr")


def script_command(name):
    """Command line for one of porthole's scripts, local or installed"""
    if LOCAL:
        return [sys.executable, os.path.join(SRC, "scripts", name), "--local"]
    return [shutil.which(name) or os.path.join("/usr/bin", name)]


def icon_path():
    for base in (os.path.join(SRC, "porthole"), "/usr/share/porthole"):
        path = os.path.join(base, "pixmaps", "porthole-icon.png")
        if os.path.exists(path):
            return path
    return None


def runner_tokens(command, pretend=False):
    """The tokens of a Porthole emerge command line that should run in the
    root job runner, or None to run it here as the user: pretend, help,
    info, search and sync runs, and anything that isn't emerge.
    e.g. 'USE="gtk" sudo -p "Password: " emerge =cat/pkg-1'
         -> ['USE=gtk', 'emerge', '=cat/pkg-1']"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    # the old way of getting root; the runner does that now
    for i in range(len(tokens) - 2):
        if tokens[i:i + 3] == ["sudo", "-p", "Password: "]:
            del tokens[i:i + 3]
            break
    if "emerge" not in tokens:
        return None
    at = tokens.index("emerge")
    if any("=" not in t for t in tokens[:at]):  # only VAR=value before emerge
        return None
    rest = tokens[at + 1:]
    short = "".join(t[1:] for t in rest if t.startswith("-") and not t.startswith("--"))
    if (pretend or "p" in short or "s" in short or "S" in short or rest[:1] == ["info"]
            or any(t in ("--pretend", "--help", "--info", "--sync", "--search",
                         "--searchdesc", "--version") for t in rest)):
        return None
    return tokens
