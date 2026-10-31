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


class SystemsWindow(Gtk.Window):
    """The systems built here for other computers: a list, New, Remove"""

    def __init__(self, mainwindow=None):
        super().__init__(title="Porthole: systems for other computers")
        self.mainwindow = mainwindow
        self.set_default_size(640, 360)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=12)
        intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
        intro.set_markup(
            "<small>Each system is built in a folder of its own on this computer, with "
            "its own Portage settings, from the profile of the computer it is for. "
            "It uses this computer's repositories, synced here.</small>")
        box.pack_start(intro, False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str)  # name, title, profile, folder
        self.view = Gtk.TreeView(model=self.store)
        for i, heading in enumerate(("Name", "Computer", "Profile")):
            column = Gtk.TreeViewColumn(heading, Gtk.CellRendererText(), text=i)
            column.set_resizable(True)
            self.view.append_column(column)
        self.view.set_tooltip_column(3)
        self.view.get_selection().connect("changed", lambda s: self.update_buttons())
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)
        self.empty = Gtk.Label(label="No systems yet.", no_show_all=True)
        box.pack_start(self.empty, False, False, 0)
        buttons = Gtk.Box(spacing=8)
        new = Gtk.Button(label="New system…")
        new.connect("clicked", lambda b: self.new_system())
        buttons.pack_start(new, False, False, 0)
        self.remove_button = Gtk.Button(label="Delete…")
        self.remove_button.connect("clicked", lambda b: self.remove_system())
        buttons.pack_start(self.remove_button, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.destroy())
        buttons.pack_end(close, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self.show_all()
        self.refresh()

    def refresh(self):
        import os
        from porthole.plugins.crossdev import roots
        self.store.clear()
        for name, target in sorted(roots.made_here().items()):
            try:
                link = os.readlink(os.path.join(target.portage_dir, "make.profile"))
                profile = link.split("/profiles/", 1)[-1]
            except OSError:
                profile = "(none)"
            self.store.append([name, target.title, profile, target.root])
        self.empty.set_visible(len(self.store) == 0)
        self.update_buttons()

    def selected(self):
        model, it = self.view.get_selection().get_selected()
        return model[it][0] if it else None

    def update_buttons(self):
        self.remove_button.set_sensitive(self.selected() is not None)

    def error(self, text, detail):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text=text)
        dialog.format_secondary_text(detail or "Cancelled.")
        dialog.run()
        dialog.destroy()

    def new_system(self):
        dialog = NewSystemDialog(self)
        while dialog.run() == Gtk.ResponseType.OK:
            args = dialog.command()
            if args:
                break
        else:
            dialog.destroy()
            return
        dialog.destroy()
        from porthole.updater.elevate import run_as_root, service

        def done(ok, output):
            if not ok:
                self.error("The system wasn't made", output)
            self.refresh()
        run_as_root(service("plugin", "crossdev", *args), done)

    def remove_system(self):
        name = self.selected()
        if not name:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE, text="Delete the system %s?" % name)
        dialog.format_secondary_text(
            "Everything built for it on this computer is deleted: its folder and its "
            "packages. The computer it is for keeps what is installed on it.")
        dialog.add_buttons("Keep it", Gtk.ResponseType.CANCEL, "Delete", Gtk.ResponseType.OK)
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return
        from porthole.updater.elevate import run_as_root, service

        def done(ok, output):
            if not ok:
                self.error("The system wasn't deleted", output)
                return
            # a job: the list changes when it has run
            GLib.timeout_add_seconds(3, lambda: self.refresh() and False)
        run_as_root(service("plugin", "crossdev", "remove-root", name), done)


class NewSystemDialog(Gtk.Dialog):
    """Name, the computer it is for, architecture, profile, repositories"""

    def __init__(self, parent):
        super().__init__(title="New system", transient_for=parent, modal=True)
        from porthole.plugins.crossdev import roots
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Make it", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.repos = roots.host_repos()
        self.profiles = roots.foreign_profiles(self.repos, roots.host_arch())
        grid = Gtk.Grid(row_spacing=8, column_spacing=12, border_width=12)
        self.get_content_area().add(grid)

        def row(n, text, widget):
            grid.attach(Gtk.Label(label=text, xalign=1), 0, n, 1, 1)
            grid.attach(widget, 1, n, 1, 1)
        self.title_entry = Gtk.Entry(placeholder_text="e.g. My Switch", hexpand=True)
        self.title_entry.connect("changed", self.title_changed)
        row(0, "Computer it is for:", self.title_entry)
        self.name_entry = Gtk.Entry(placeholder_text="e.g. switch", max_length=41)
        self.name_entry.set_tooltip_text("Lower case letters, digits, - and _. Its folder "
                                         "is /var/lib/porthole/roots/<name>.")
        self.name_entry.connect("changed", lambda e: self.validate())
        self.named_by_hand = False
        self.name_entry.connect("key-press-event", self.name_typed)
        row(1, "Short name:", self.name_entry)
        self.arch = Gtk.ComboBoxText()
        arches = sorted({p[1] for p in self.profiles},
                        key=lambda a: (a not in ARCHES, ARCHES.index(a) if a in ARCHES else 0, a))
        for a in arches:
            self.arch.append(a, a)
        self.arch.connect("changed", lambda c: self.fill_profiles())
        row(2, "Architecture:", self.arch)
        self.profile = Gtk.ComboBoxText()
        self.profile.connect("changed", lambda c: (self.fill_repos(), self.validate()))
        row(3, "Profile:", self.profile)
        self.experimental = Gtk.CheckButton(label="Show experimental profiles")
        self.experimental.connect("toggled", lambda b: self.fill_profiles())
        grid.attach(self.experimental, 1, 4, 1, 1)
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        hint.set_markup("<small>A board's own repository (like the Nintendo Switch "
                        "overlay) brings its profiles: add it in Settings, Repositories, "
                        "and it is listed here.</small>")
        grid.attach(hint, 1, 5, 1, 1)
        grid.attach(Gtk.Label(label="Also use:", xalign=1, yalign=0), 0, 6, 1, 1)
        self.repo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        grid.attach(self.repo_box, 1, 6, 1, 1)
        self.message = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        grid.attach(self.message, 1, 7, 1, 1)
        # most boards are arm64; a board repository's profiles come first
        self.arch.set_active_id("arm64" if "arm64" in arches else (arches or [None])[0])
        self.show_all()

    def title_changed(self, entry):
        if not self.named_by_hand:
            import re
            name = re.sub(r"[^a-z0-9_-]+", "-", entry.get_text().lower()).strip("-_")
            name = re.sub(r"^(my|the)-", "", name)[:41]
            self.name_entry.set_text(name)
        self.validate()

    def name_typed(self, entry, event):
        self.named_by_hand = True
        return False

    def fill_profiles(self):
        arch = self.arch.get_active_id()
        show_exp = self.experimental.get_active()
        self.profile.remove_all()
        for repo, a, name, status in self.profiles:
            if a == arch and (show_exp or status != "exp"):
                label = "%s: %s%s" % (repo, name, "" if status == "stable" else
                                      " (%s)" % status)
                self.profile.append("%s:%s" % (repo, name), label)
        self.profile.set_active(0)

    def fill_repos(self):
        for child in self.repo_box.get_children():
            child.destroy()
        chosen = (self.profile.get_active_id() or ":").split(":")[0]
        for name in sorted(self.repos):
            if name in ("gentoo", chosen):
                continue
            check = Gtk.CheckButton(label=name)
            check.set_tooltip_text(self.repos[name])
            self.repo_box.pack_start(check, False, False, 0)
        if not self.repo_box.get_children():
            self.repo_box.pack_start(Gtk.Label(label="(no other repositories)", xalign=0),
                                     False, False, 0)
        self.repo_box.show_all()

    def validate(self):
        from porthole.updater import targets
        name = self.name_entry.get_text()
        problem = None
        if not name:
            problem = "Give it a short name."
        elif not targets._NAME.match(name) or name == targets.LOCAL:
            problem = "The short name can have lower case letters, digits, - and _."
        elif name in targets.load():
            problem = "There already is a system called %s." % name
        elif not self.profile.get_active_id():
            problem = "Choose a profile."
        self.message.set_markup("<small>%s</small>" % GLib.markup_escape_text(problem or ""))
        self.set_response_sensitive(Gtk.ResponseType.OK, problem is None)
        return problem is None

    def command(self):
        if not self.validate():
            return None
        args = ["new-root", self.name_entry.get_text(), "--profile",
                self.profile.get_active_id()]
        if self.title_entry.get_text().strip():
            args += ["--title", self.title_entry.get_text().strip()]
        for check in self.repo_box.get_children():
            if isinstance(check, Gtk.CheckButton) and check.get_active():
                args += ["--repo", check.get_label()]
        return args
