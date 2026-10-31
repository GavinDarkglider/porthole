"""
    Porthole crossdev plugin: is this computer ready?

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole.plugins.crossdev import checks

ARCHES = ("arm64", "arm", "riscv", "ppc64", "x86", "loong")


class ReadinessWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Porthole: ready to build for another computer?")
        self.set_default_size(720, 520)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=12)
        intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=90)
        intro.set_markup(
            "<small>What building a system for another architecture needs on this "
            "computer. Cross-compiling with crossdev comes first; qemu and binfmt are "
            "needed only for packages that won't cross-compile, and distcc lets those "
            "still compile natively.</small>")
        box.pack_start(intro, False, False, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Architecture of the other computer:"), False, False, 0)
        self.arch = Gtk.ComboBoxText()
        for arch in ARCHES:
            self.arch.append(arch, arch)
        self.arch.set_active_id("arm64")
        self.arch.connect("changed", lambda c: self.refresh())
        row.pack_start(self.arch, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(self.list)
        box.pack_start(scroller, True, True, 0)
        close = Gtk.Button(label="Close", halign=Gtk.Align.END)
        close.connect("clicked", lambda b: self.destroy())
        box.pack_start(close, False, False, 0)
        self.add(box)
        self.show_all()
        self.refresh()

    def refresh(self):
        for child in self.list.get_children():
            child.destroy()
        arch = self.arch.get_active_id()
        threading.Thread(target=lambda: GLib.idle_add(self.show_checks,
                                                      checks.check_all(arch)),
                         daemon=True).start()

    def show_checks(self, results):
        for c in results:
            mark = {True: "✔", False: "✘", None: "?"}[c.ok]
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=90)
            text = "%s  <b>%s</b>: %s" % (mark, GLib.markup_escape_text(c.title),
                                          GLib.markup_escape_text(c.found))
            if c.fix:
                text += "\n<small>%s</small>" % GLib.markup_escape_text(c.fix)
            label.set_markup(text)
            self.list.pack_start(label, False, False, 0)
        self.list.show_all()
        return False
