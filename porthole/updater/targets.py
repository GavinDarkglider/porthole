"""
    Porthole update assistant: the systems Porthole manages ("targets")

    Portage works on a config root (where etc/portage is: profile,
    make.conf, package.*) and a root (where packages are installed). This
    computer is config root / and root /. Others are the same idea with
    other paths: a crossdev environment (/usr/<CTARGET>), a chroot for
    another machine, later a remote one. Plans, choices, fixes and jobs
    each belong to a target; there is still one job runner and one queue
    (one CPU, one build window).

    Targets other than this computer are listed in /etc/porthole/targets.conf
    (root's to change):

        [arm-board]
        title = The kitchen Raspberry Pi
        config_root = /usr/aarch64-unknown-linux-gnu
        root = /usr/aarch64-unknown-linux-gnu

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import configparser
import os
import re

TARGETS_FILE = "/etc/porthole/targets.conf"
LOCAL = "local"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class Target:
    def __init__(self, name, title, config_root="/", root="/", sysroot=None):
        self.name = name
        self.title = title
        self.config_root = config_root
        self.root = root
        self.sysroot = sysroot or root

    def __repr__(self):
        return "Target(%r, config_root=%r, root=%r)" % (self.name, self.config_root, self.root)

    @property
    def local(self):
        return self.name == LOCAL

    @property
    def portage_dir(self):
        """Its /etc/portage"""
        return os.path.join(self.config_root, "etc", "portage")

    def emerge_args(self):
        """emerge options choosing this system (none for this computer)"""
        args = []
        if self.config_root != "/":
            args.append("--config-root=" + self.config_root)
        if self.root != "/":
            args.append("--root=" + self.root)
        if self.sysroot != "/":
            args.append("--sysroot=" + self.sysroot)
        return args

    def state_dir(self, base):
        """Where its plans and choices live, under the service's state dir
        (this computer's stay where they always were)"""
        return base if self.local else os.path.join(base, "targets", self.name)


THIS_COMPUTER = Target(LOCAL, "This computer")


def load(path=TARGETS_FILE):
    """{name: Target}: this computer, then the ones in targets.conf"""
    targets = {LOCAL: THIS_COMPUTER}
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error:
        return targets
    for name in parser.sections():
        section = parser[name]
        root = section.get("root", "").strip()
        config_root = section.get("config_root", root).strip()
        if (not _NAME.match(name) or name == LOCAL or not os.path.isabs(root)
                or not os.path.isabs(config_root) or root == "/"):
            continue  # not usable; this computer is never redefined
        targets[name] = Target(name, section.get("title", name).strip() or name,
                               config_root, root,
                               section.get("sysroot", "").strip() or None)
    return targets


def get(name=None):
    """A target by name (default this computer); KeyError if unknown"""
    return load()[name or LOCAL]


def of_plan(plan):
    """The target a plan was made for (older plans: this computer)"""
    try:
        return get((plan or {}).get("target"))
    except KeyError:
        return THIS_COMPUTER
