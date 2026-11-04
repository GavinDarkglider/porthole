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
from porthole.updater.targets import LOCAL

QUEUE_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "porthole", "install-queue.json")

SETTING_FILES = {"use": "package.use", "keywords": "package.accept_keywords",
                 "mask": "package.mask"}


OFFERED_FILE = os.path.join(os.path.dirname(QUEUE_FILE), "unfinished-offered.json")


class InstallQueue:
    """[{"name", "targets": [options and atoms], "requested": [settings]}],
    plus for an install that stopped part way (see resume.py) "resume":
    {"job", "started", "packages", "total"} and "world": the packages to
    add to the world file. "system": the updater.targets name it is for,
    when not this computer (the main window was showing another system)."""

    def __init__(self, path=QUEUE_FILE, offered_path=OFFERED_FILE):
        self.path = path
        self.offered_path = offered_path
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

    def add(self, name, targets, requested=(), system=None):
        system = system if system and system != LOCAL else None
        atoms = [t for t in targets if not t.startswith("-")]
        # the same packages again (for the same system) replace their earlier entry
        self.items = [i for i in self.items
                      if [t for t in i["targets"] if not t.startswith("-")] != atoms
                      or i.get("system") != system]
        item = {"name": name, "targets": list(targets), "requested": list(requested)}
        if system:
            item["system"] = system
        self.items.append(item)
        self.changed()

    def systems(self):
        """The systems queued items are for, in order (LOCAL: this computer)"""
        return list(dict.fromkeys(i.get("system") or LOCAL for i in self.items))

    def items_for(self, system=LOCAL):
        return [i for i in self.items if (i.get("system") or LOCAL) == system]

    def prune(self):
        """Drop what unfinished installs have had built since; True if
        anything changed"""
        from porthole.updater import resume
        before = json.dumps(self.items)
        self.items = [i for i in self.items if resume.refresh(i)]
        return json.dumps(self.items) != before

    def add_unfinished(self, jobs):
        """Add entries carrying on with this user's installs that stopped
        part way, each job once (removing its entry is the answer "no").
        The first time, only the newest is offered. Returns their names."""
        from porthole.updater import resume
        try:
            with open(self.offered_path) as f:
                offered = set(json.load(f))
            first = False
        except (OSError, ValueError):
            offered, first = set(), True
        found = [(job_id, item) for job_id, item in resume.unfinished(jobs)
                 if job_id not in offered]
        if first:
            found = found[:1]
            offered |= {j["id"] for j in jobs if j.get("status") in resume.STOPPED}
        added = []
        for job_id, item in found:
            offered.add(job_id)
            packages = set(item["resume"]["packages"])
            # an earlier unfinished entry for the same packages is replaced
            self.items = [i for i in self.items
                          if set((i.get("resume") or {}).get("packages") or ()) != packages]
            self.items.append(item)
            added.append(item["name"])
        try:
            os.makedirs(os.path.dirname(self.offered_path), exist_ok=True)
            with open(self.offered_path, "w") as f:
                json.dump(sorted(offered), f)
        except OSError:
            pass
        if added:
            self.changed()
        return added

    def remove(self, index):
        del self.items[index]
        self.changed()

    def clear(self, system=None):
        """Empty the queue, or only what is queued for system"""
        self.items = [] if system is None else [
            i for i in self.items if (i.get("system") or LOCAL) != system]
        self.changed()

    def combined(self):
        """(targets, requested, world) for installing everything in one go.

        --oneshot applies to a whole emerge, so when only some entries are
        one-shot, targets has --oneshot and world lists the packages of the
        other entries: all are planned together, then the one-shot packages
        are emerged first and the world ones after (Analyzer.install_commands)"""
        if self.prune():
            self.changed()
        options, atoms, requested, lines = [], [], [], set()
        oneshot, world = [], []
        for item in self.items_for(LOCAL):
            mine = [t for t in item["targets"] if not t.startswith("-")]
            one = "--oneshot" in item["targets"] or "-1" in item["targets"]
            if item.get("world"):
                # an unfinished install: one-shot, but some for the world file
                world.extend(a for a in item["world"] if a not in world)
                mine = [a for a in mine if a not in item["world"]]
            for t in item["targets"]:
                if t in ("--oneshot", "-1"):
                    continue
                bucket = options if t.startswith("-") else atoms
                if t not in bucket:
                    bucket.append(t)
            (oneshot if one else world).extend(a for a in mine if a not in world)
            for r in item["requested"]:
                if r["line"] not in lines:
                    lines.add(r["line"])
                    requested.append(r)
        world = [a for a in world if a not in oneshot]
        if oneshot or any(i.get("world") for i in self.items_for(LOCAL)):
            options.append("--oneshot")
        else:
            world = []  # emerge adds them itself
        return options + atoms, requested, world


def added_popup(parent, name, count, open_queue, detail=None, title=None, for_system=None):
    """A short note that a package went into the queue; it closes itself"""
    dialog = Gtk.MessageDialog(transient_for=parent, modal=False,
                               message_type=Gtk.MessageType.INFO,
                               buttons=Gtk.ButtonsType.NONE,
                               text=title or "%s added to the install queue" % name)
    dialog.set_title("Porthole")
    dialog.format_secondary_text(("For %s. " % for_system if for_system else "") + (detail or (
        "%d package%s queued. Nothing is installed until you process the queue "
        "(the Queue button) and approve the plan." % (count, "s" if count != 1 else ""))))
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
    def __init__(self, queue, install, install_into=None, shown=None):
        """install(name, targets, requested, on_queued, world) plans and
        starts this computer's part of the queue; install_into(system,
        items, on_queued) another system's. shown() is the system the main
        window shows (its part is processed first). on_queued empties
        that part."""
        super().__init__(title="Porthole: install queue")
        if icon_path():
            self.set_icon_from_file(icon_path())
        self.set_default_size(720, 360)
        self.queue = queue
        self.install = install
        self.install_into = install_into
        self.shown = shown or (lambda: LOCAL)
        self.connect("delete-event", lambda *a: self.hide() or True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
        self.heading = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(self.heading, False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, title in enumerate(("Package", "Install", "Settings", "For")):
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
        if self.queue.prune():
            self.queue.changed()  # calls refresh again
            return
        self.store.clear()
        for item in self.queue.items:
            settings = "; ".join("%s: %s" % (SETTING_FILES.get(r["kind"], r["kind"]), r["line"])
                                 for r in item["requested"])
            name = item["name"]
            info = item.get("resume")
            if info:
                name += " (%d of %d left)" % (len(info["packages"]), info["total"])
            self.store.append([name, " ".join(item["targets"]), settings,
                               system_title(item.get("system") or LOCAL)])
        self.view.get_column(3).set_visible(self.queue.systems() not in ([], [LOCAL]))
        count = len(self.queue.items)
        self.heading.set_text(
            "Nothing is queued. Emerge, Advanced Emerge and Upgrade add packages here."
            if not count else
            "%d package%s queued. Process queue works out what they need together "
            "and shows it for review; nothing is installed until you press Install "
            "there."
            % (count, "s" if count != 1 else ""))
        self.install_button.set_sensitive(bool(count))
        system = self.next_system()
        self.install_button.set_label(
            "Process queue..." if system in (None, LOCAL)
            else "Process queue for %s..." % system_title(system))

    def on_remove(self, button):
        model, _iter = self.view.get_selection().get_selected()
        if _iter is not None:
            self.queue.remove(model.get_path(_iter).get_indices()[0])

    def next_system(self):
        """The part of the queue Process queue works on: the shown system's
        if anything is queued for it, else the first queued"""
        systems = self.queue.systems()
        if not systems:
            return None
        return self.shown() if self.shown() in systems else systems[0]

    def on_install(self, button):
        system = self.next_system()
        if system is None:
            return
        self.hide()
        if system != LOCAL:
            self.install_into(system, self.queue.items_for(system),
                              lambda: self.queue.clear(system))
            return
        targets, requested, world = self.queue.combined()
        names = ", ".join(item["name"] for item in self.queue.items_for(LOCAL))
        self.install(names, targets, requested, lambda: self.queue.clear(LOCAL), world)


def system_title(system):
    if system == LOCAL:
        return "This computer"
    from porthole.updater import targets
    target = targets.load().get(system)
    return target.title if target else system
