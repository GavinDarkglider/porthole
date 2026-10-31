"""
    Porthole: Settings > Plugins

    The plugins installed with Porthole, and which are switched on for this
    computer (plugins = ... in updater.conf, saved as root: a plugin may run
    things as root). A change takes effect when Porthole starts again.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole import plugins
from porthole.updater.elevate import run_as_root, service
from porthole.updater.repospage import _small
from porthole.updater.service import load_config


class PluginsPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)
        self.saved = sorted(load_config().get("plugins") or [])
        self.pack_start(_small(
            "Plugins add to Porthole. They are installed with it (some by USE flag) and "
            "switched on here for the whole computer, because a plugin may run things as "
            "root. Changing this asks for administrator rights; it takes effect when "
            "Porthole starts again."), False, False, 0)
        self.checks = {}
        names = plugins.installed()
        if not names:
            self.pack_start(_small("No plugins are installed."), False, False, 0)
        for name in names:
            title, text = plugins.describe(name)
            check = Gtk.CheckButton()
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
            label.set_markup("<b>%s</b>\n<small>%s</small>" % (
                GLib.markup_escape_text(title), GLib.markup_escape_text(text)))
            check.add(label)
            check.set_active(name in self.saved)
            self.checks[name] = check
            self.pack_start(check, False, False, 0)

    def values(self):
        return sorted(name for name, check in self.checks.items() if check.get_active())

    def save(self, callback=None):
        values = self.values()
        if values == self.saved:
            if callback:
                callback(True, "")
            return

        def done(ok, output):
            if ok:
                self.saved = values
            if callback:
                callback(ok, output)
        run_as_root(service("config", "plugins=" + " ".join(values)), done)
