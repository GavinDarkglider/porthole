#!/usr/bin/env python

'''
    Porthole Reader Class: Sets List Reader

    Copyright (C) 2003 - 2007 Fredrik Arnerup, Brian Dolbec,
    Daniel G. Taylor and Wm. F. Wheeler, Tommy Iorns

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
'''
import os
from gettext import gettext as _

from porthole.utils import debug
from porthole import db
from porthole.db.package import Package
from porthole.readers.commonreader import CommonReader

# the Sets view's groups, in order; a set in none of them goes in "Other"
# (that includes your own, from /etc/portage/sets)
SET_GROUPS = [
    (_("Main"), ["world", "selected", "system", "profile", "installed"]),
    (_("Fixes and rebuilds"), ["security", "preserved-rebuild", "smart-live-rebuild", "live-rebuild",
                     "deprecated-live-rebuild", "changed-subslot", "changed-deps",
                     "module-rebuild", "x11-module-rebuild", "golang-rebuild",
                     "rust-rebuild", "rebuilt-binaries"]),
]
OTHER = _("Other")
# the same as @selected, split
SKIPPED_SETS = {"selected-packages", "selected-sets"}
# only root can work this out (portage's list of old libraries in use):
# the root update check lists it, and an unlocked Porthole asks root
FROM_ROOT_CHECK = {"preserved-rebuild"}
# slow to work out (@module-rebuild reads every installed file list):
# worked out when opened in the view (see packages_of)
LAZY_SETS = {"module-rebuild", "unavailable", "changed-subslot", "downgrade", "changed-deps",
             "security", "smart-live-rebuild"}
# the category tree nests on "-", so names keep theirs as U+2011
NB_HYPHEN = "\u2011"


def category(group, name):
    return "%s-@%s" % (group, name.replace("-", NB_HYPHEN))


def set_packages(config, name):
    """The package names in a set (nested sets included)"""
    if name in FROM_ROOT_CHECK:
        # as they are now, if Porthole is unlocked
        from porthole.updater.session import root_sets
        live = root_sets([name])
        if live is not None:
            return set(live.get(name) or ())
        # else root's own plan (the user can't see it)
        from porthole.updater.service import PLAN_FILE, read_json
        plan = read_json(PLAN_FILE) or {}
        return {m["cp"] for m in (plan.get("preserved") or {}).get("merge_list") or []}
    if name == "smart-live-rebuild":
        # portage's set refuses to run as the user; asked the way the
        # update check asks (it only reads the upstream repositories)
        from portage.dep import Atom
        from porthole.updater.analyze import smart_live_rebuild
        atoms, error = smart_live_rebuild()
        if error:
            debug.dprint("READERS: SetListReader(); @%s: %s" % (name, error))
        return {Atom(a).cp for a in atoms}
    try:
        return {atom.cp for atom in config.getSetAtoms(name)}
    except Exception as e:
        debug.dprint("READERS: SetListReader(); @%s: %s" % (name, e))
        return set()


def packages_of(name, config=None):
    """{name: Package} for a set, for the package view"""
    if config is None and name not in FROM_ROOT_CHECK and name != "smart-live-rebuild":
        import portage
        from portage._sets import load_default_config
        config = load_default_config(portage.settings, portage.db[portage.root])
    return {cp: db.db.get_package(cp) or Package(cp)
            for cp in sorted(set_packages(config, name))}


class SetListReader(CommonReader):
    """ Portage's package sets (@world, @system, the rebuild sets, your
    own...) as categories of packages """
    def __init__( self ):
        """ Initialize """
        CommonReader.__init__(self)
        self.reader_type = "Sets"
        # hack for statusbar updates
        self.progress = 2
        self.pkg_dict = {}
        self.pkg_count = {}
        self.count = 0
        self.pkg_dict_total = 0
        # {category: set name} of the sets left to work out when opened
        self.lazy = {}

    def run( self ):
        """fill SETS tree"""
        debug.dprint("READERS: SetListReader(); process id = %d *******************" %os.getpid())
        import portage
        from portage._sets import load_default_config
        config = load_default_config(portage.settings, portage.db[portage.root])
        names = sorted(n for n in config.getSets() if n not in SKIPPED_SETS)
        grouped = [(group, [n for n in members if n in names]) for group, members in SET_GROUPS]
        known = {n for _group, members in grouped for n in members}
        grouped.append((OTHER, [n for n in names if n not in known]))
        for group, members in grouped:
            for name in members:
                if self.cancelled: self.done = True; return
                key = category(group, name)
                if name in LAZY_SETS:
                    self.lazy[key] = name
                    self.pkg_dict[key] = {}
                    self.pkg_count[key] = None  # not known yet
                    continue
                self.pkg_dict[key] = packages_of(name, config)
                self.pkg_count[key] = len(self.pkg_dict[key])
                self.count += self.pkg_count[key]
        self.pkg_dict_total = 0
        for key in self.pkg_count:
            if key in self.lazy:
                continue
            self.pkg_dict_total += self.pkg_count[key]
            if self.pkg_dict[key] == {}:
                pkg = Package(_("None"))
                self.pkg_dict[key][_("None")] = pkg
        # set the thread as finished
        self.done = True
        return
