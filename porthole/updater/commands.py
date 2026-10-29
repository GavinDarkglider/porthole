"""
    Porthole update assistant: locating porthole's own programs

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
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
