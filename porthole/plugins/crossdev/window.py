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
    def __init__(self, mainwindow=None):
        super().__init__(title="Porthole: ready to build for another computer?")
        # the main window's install queue: installs are planned and reviewed there
        self.mainwindow = mainwindow
        self.results = []
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
        buttons = Gtk.Box(spacing=8)
        self.all_button = Gtk.Button(label="Set up everything missing",
                                     no_show_all=True)
        self.all_button.set_tooltip_text(
            "Does what Porthole can do here now and adds the installs to the install "
            "queue, where they are planned and shown to you before anything is built.")
        self.all_button.connect("clicked", lambda b: self.fix_all())
        buttons.pack_start(self.all_button, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.destroy())
        buttons.pack_end(close, False, False, 0)
        box.pack_start(buttons, False, False, 0)
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
        self.results = results
        for child in self.list.get_children():
            child.destroy()
        for c in results:
            mark = {True: "✔", False: "✘", None: "?"}[c.ok]
            row = Gtk.Box(spacing=8)
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
            text = "%s  <b>%s</b>: %s" % (mark, GLib.markup_escape_text(c.title),
                                          GLib.markup_escape_text(c.found))
            if c.fix:
                text += "\n<small>%s</small>" % GLib.markup_escape_text(c.fix)
            label.set_markup(text)
            row.pack_start(label, True, True, 0)
            action = self.usable(c.action)
            if action and self.queued(action):
                row.pack_end(Gtk.Label(label="In the install queue"), False, False, 0)
            elif action:
                button = Gtk.Button(label="Add to the install queue" if "queue" in action
                                    else "Create it", valign=Gtk.Align.CENTER)
                button.connect("clicked", lambda b, a=action: self.fix(a))
                row.pack_end(button, False, False, 0)
            self.list.pack_start(row, False, False, 0)
        self.list.show_all()
        self.all_button.set_visible(sum(bool(self.usable(c.action))
                                        and not self.queued(c.action) for c in results) > 1)
        return False

    def usable(self, action):
        """The action, if this window can carry it out"""
        if action and "queue" in action and self.mainwindow is None:
            return None
        return action

    def queued(self, action):
        """Whether this install is in the install queue already"""
        if "queue" not in (action or {}) or self.mainwindow is None:
            return False
        wanted = action["queue"]["targets"]
        return any(i["targets"] == wanted for i in self.mainwindow.install_queue.items)

    def fix(self, action, then=None):
        if "queue" in action:
            item = action["queue"]
            queue = self.mainwindow.install_queue
            queue.add(item["name"], item["targets"], item["requested"])
            if then is None:
                from porthole.updater.installqueue import added_popup
                added_popup(self, item["name"], len(queue.items),
                            self.mainwindow.show_install_queue)
            self.show_checks(self.results)
            if then:
                then()
            return
        from porthole.updater.elevate import run_as_root, service

        def done(ok, output):
            if not ok:
                dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                           message_type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.CLOSE,
                                           text="That wasn't done")
                dialog.format_secondary_text(output or "Cancelled.")
                dialog.run()
                dialog.destroy()
            self.refresh()
            if ok and then:
                then()
        run_as_root(service("plugin", "crossdev", *action["root"]), done)

    def fix_all(self):
        """Root changes first (one password), then the installs, then the
        install queue to process"""
        actions = [a for a in (self.usable(c.action) for c in self.results)
                   if a and not self.queued(a)]
        actions.sort(key=lambda a: "queue" in a)

        def step():
            if not actions:
                if self.mainwindow is not None:
                    self.mainwindow.show_install_queue()
                return
            self.fix(actions.pop(0), then=step)
        step()
