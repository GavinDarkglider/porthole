"""
    Porthole: the install queue

    Emerge, Advanced Emerge and Upgrade add packages (with their emerge
    options and chosen settings) here instead of running anything; a
    popup says so. When the user processes the queue, it becomes one
    request: the runner plans all of it together (dependencies shared
    between the packages are worked out once), the plan is always shown
    for review, and only Install there starts the one job. The queue is
    kept per user and survives restarting Porthole; it is emptied only
    once its job is queued.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import os

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole.updater.commands import icon_path

QUEUE_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "porthole", "install-queue.json")

SETTING_FILES = {"use": "package.use", "keywords": "package.accept_keywords",
                 "mask": "package.mask"}


class InstallQueue:
    """[{"name", "targets": [options and atoms], "requested": [settings]}]"""

    def __init__(self, path=QUEUE_FILE):
        self.path = path
        self.listeners = []
        try:
            with open(path) as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = []

    def changed(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.items, f, indent=1)
        except OSError:
            pass
        for listener in self.listeners:
            listener()

    def add(self, name, targets, requested=()):
        atoms = [t for t in targets if not t.startswith("-")]
        # the same packages again replace their earlier entry
        self.items = [i for i in self.items
                      if [t for t in i["targets"] if not t.startswith("-")] != atoms]
        self.items.append({"name": name, "targets": list(targets),
                           "requested": list(requested)})
        self.changed()

    def remove(self, index):
        del self.items[index]
        self.changed()

    def clear(self):
        self.items = []
        self.changed()

    def combined(self):
        """(targets, requested) for installing everything in one go"""
        options, atoms, requested, lines = [], [], [], set()
        for item in self.items:
            for t in item["targets"]:
                bucket = options if t.startswith("-") else atoms
                if t not in bucket:
                    bucket.append(t)
            for r in item["requested"]:
                if r["line"] not in lines:
                    lines.add(r["line"])
                    requested.append(r)
        return options + atoms, requested


def added_popup(parent, name, count, open_queue):
    """A short note that a package went into the queue; it closes itself"""
    dialog = Gtk.MessageDialog(transient_for=parent, modal=False,
                               message_type=Gtk.MessageType.INFO,
                               buttons=Gtk.ButtonsType.NONE,
                               text="%s added to the install queue" % name)
    dialog.set_title("Porthole")
    dialog.format_secondary_text(
        "%d package%s queued. Nothing is installed until you process the queue "
        "(the Queue button) and approve the plan." % (count, "s" if count != 1 else ""))
    dialog.add_buttons("Open queue", 1, "OK", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)

    closed = []

    def response(d, r):
        closed.append(True)
        d.destroy()
        if r == 1:
            open_queue()

    def timeout():
        if not closed:
            dialog.response(Gtk.ResponseType.OK)
        return False
    dialog.connect("response", response)
    GLib.timeout_add_seconds(6, timeout)
    dialog.show_all()
    return dialog


class InstallQueueWindow(Gtk.Window):
    def __init__(self, queue, install):
        """install(name, targets, requested, on_queued) plans and starts
        the queue; on_queued empties it"""
        super().__init__(title="Porthole: install queue")
        if icon_path():
            self.set_icon_from_file(icon_path())
        self.set_default_size(720, 360)
        self.queue = queue
        self.install = install
        self.connect("delete-event", lambda *a: self.hide() or True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
        self.heading = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(self.heading, False, False, 0)
        self.store = Gtk.ListStore(str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, title in enumerate(("Package", "Install", "Settings")):
            cell = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, cell, text=i)
            column.set_resizable(True)
            self.view.append_column(column)
        scroller = Gtk.ScrolledWindow()
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        remove = Gtk.Button(label="Remove")
        remove.connect("clicked", self.on_remove)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda b: self.queue.clear())
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.hide())
        self.install_button = Gtk.Button(label="Process queue...")
        self.install_button.set_tooltip_text(
            "Work out what the queued packages need and show it for review; "
            "nothing is installed until you press Install there")
        self.install_button.get_style_context().add_class("suggested-action")
        self.install_button.connect("clicked", self.on_install)
        # Close away from Process queue, so one isn't pressed for the other
        buttons.pack_start(close, False, False, 0)
        buttons.pack_start(remove, False, False, 12)
        buttons.pack_start(clear, False, False, 0)
        buttons.pack_end(self.install_button, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        queue.listeners.append(self.refresh)
        self.refresh()
        box.show_all()

    def refresh(self):
        self.store.clear()
        for item in self.queue.items:
            settings = "; ".join("%s: %s" % (SETTING_FILES.get(r["kind"], r["kind"]), r["line"])
                                 for r in item["requested"])
            self.store.append([item["name"], " ".join(item["targets"]), settings])
        count = len(self.queue.items)
        self.heading.set_text(
            "Nothing is queued. Emerge, Advanced Emerge and Upgrade add packages here."
            if not count else
            "%d package%s queued. Process queue works out what they need together "
            "and shows it for review; nothing is installed until you press Install "
            "there."
            % (count, "s" if count != 1 else ""))
        self.install_button.set_sensitive(bool(count))

    def on_remove(self, button):
        model, _iter = self.view.get_selection().get_selected()
        if _iter is not None:
            self.queue.remove(model.get_path(_iter).get_indices()[0])

    def on_install(self, button):
        targets, requested = self.queue.combined()
        names = ", ".join(item["name"] for item in self.queue.items)
        self.hide()
        self.install(names, targets, requested, self.queue.clear)
