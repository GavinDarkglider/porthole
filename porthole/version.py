#!/usr/bin/env python
"""
    Porthole version
    Copyright (C) 2006 -2011 Brian Dolbec


    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program; if not, write to the Free Software
    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""
from gettext import gettext as _
import os

#version = "0.6.1"

copyright = _("Copyright (c) 2003 - 2020")

version ="git-"

ver_info = {}


def get_git_info(prop):
    global ver_info
    if ver_info == {}:
        commit = b''
        date = b''
        branch = b'unknown'
        # ask the checkout this file lives in, not the current directory;
        # safe.directory lets root (pkexec) read a user's checkout
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        git = ["git", "-C", src, "-c", "safe.directory=" + src]
        try:
            from subprocess import DEVNULL, PIPE, run
            data = run(git + ["log", "-1"], stdout=PIPE,
                       stderr=DEVNULL).stdout.split(b'\n')
            branches = run(git + ["branch"], stdout=PIPE,
                           stderr=DEVNULL).stdout.split(b'\n')
        except OSError:
            print("Error obtaining git log and branch info")
            data = branches = []

        for item in data:
            if item.startswith(b'commit'):
                commit = item.split()[-1]
            elif item.startswith(b'Date'):
                date = item.split(b":", 1)[-1].lstrip()

        current = [x.split()[-1].strip() for x in branches if x.startswith(b'*')]
        if current:
            branch = current[0]

        ver_info['commit'] = commit[:10].decode()
        ver_info['date'] = date.decode()
        ver_info['branch'] = branch.decode()

    return ver_info[prop]

def get_version():
    global version
    if 'git' in version:
        rev = get_git_info('commit')
        # pkexec doesn't set LOGNAME
        login = os.getenv("LOGNAME") or os.getenv("USER") or ""
        version = version + get_git_info('branch') + "-rev:" + rev + "--"+login
    return version

version = get_version()
