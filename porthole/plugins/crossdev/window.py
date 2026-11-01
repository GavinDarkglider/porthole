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
from gi.repository import Gdk, GLib, Gtk

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
        # name, title, profile, folder, keeps binary packages
        self.store = Gtk.ListStore(str, str, str, str, bool)
        self.view = Gtk.TreeView(model=self.store)
        for i, heading in enumerate(("Name", "Computer", "Profile")):
            column = Gtk.TreeViewColumn(heading, Gtk.CellRendererText(), text=i)
            column.set_resizable(True)
            self.view.append_column(column)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.binpkgs_toggled)
        column = Gtk.TreeViewColumn("Keeps packages", toggle, active=4)
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
        self.settings_button = Gtk.Button(label="Settings…")
        self.settings_button.set_tooltip_text(
            "Everything it is built with: profile, compiler, USE flags, hardware, "
            "build options and its Portage files")
        self.settings_button.connect("clicked", lambda b: self.open_settings(self.selected()))
        buttons.pack_start(self.settings_button, False, False, 0)
        self.view.connect("row-activated", lambda v, p, c: self.open_settings(self.selected()))
        self.toolchain_button = Gtk.Button(label="Build its toolchain…")
        self.toolchain_button.set_tooltip_text(
            "The cross compiler for it: the versions of gcc, binutils, the C library and "
            "kernel headers its profile allows, built here with crossdev.")
        self.toolchain_button.connect("clicked", lambda b: self.build_toolchain())
        buttons.pack_start(self.toolchain_button, False, False, 0)
        self.build_button = Gtk.Button(label="Build the system…")
        self.build_button.set_tooltip_text(
            "Cross-compile it here: kernel headers and C library, @system, its package list, "
            "then its first-boot setup. Shows what it builds (and anything it needs changed "
            "on this computer) first.")
        self.build_button.connect("clicked", lambda b: self.build_system())
        buttons.pack_start(self.build_button, False, False, 0)
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
            self.store.append([name, target.title, profile, target.root,
                               roots.keeps_binpkgs(target)])
        self.empty.set_visible(len(self.store) == 0)
        self.update_buttons()

    def binpkgs_toggled(self, renderer, path):
        """Keep binary packages of what is built (to install them on the
        computer it is for, or host them), or just build the system"""
        name, keep = self.store[path][0], not self.store[path][4]
        from porthole.updater.elevate import run_as_root, service

        def done(ok, output):
            if not ok:
                self.error("That wasn't changed", output)
            self.refresh()
        run_as_root(service("plugin", "crossdev", "binpkgs", name, "yes" if keep else "no"),
                    done)

    def selected(self):
        model, it = self.view.get_selection().get_selected()
        return model[it][0] if it else None

    def update_buttons(self):
        self.remove_button.set_sensitive(self.selected() is not None)
        self.settings_button.set_sensitive(self.selected() is not None)
        self.build_button.set_sensitive(self.selected() is not None)
        self.toolchain_button.set_sensitive(self.selected() is not None)

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
            if ok:
                # set it up before anything is built
                self.open_settings(args[1])
        run_as_root(service("plugin", "crossdev", *args), done)

    def open_settings(self, name):
        if not name:
            return
        from porthole.plugins.crossdev.settingswin import SystemSettingsWindow
        SystemSettingsWindow(name, self, on_changed=self.refresh)

    def build_toolchain(self):
        """Show what its profile allows (worked out as the user), then
        queue the build"""
        from porthole.plugins.crossdev import roots, toolchain as tc
        name = self.selected()
        target = roots.made_here().get(name)
        if target is None:
            return
        self.toolchain_button.set_sensitive(False)

        def work():
            try:
                resolved = tc.resolve(target)
                GLib.idle_add(self.confirm_toolchain, target, resolved, None)
            except Exception as e:
                GLib.idle_add(self.confirm_toolchain, target, None, str(e))
        threading.Thread(target=work, daemon=True).start()

    def confirm_toolchain(self, target, resolved, problem):
        from porthole.plugins.crossdev import toolchain as tc
        self.update_buttons()
        if problem:
            self.error("Porthole couldn't work out the toolchain", problem)
            return False
        if not tc.needs_cross(resolved):
            dialog = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.CLOSE,
                text="%s needs no cross toolchain" % target.title)
            dialog.format_secondary_text(
                "It is for %s, this computer's own, so this computer's compiler builds "
                "for it." % resolved["tuple"])
            dialog.run()
            dialog.destroy()
            return False
        rows = tc.compare(resolved)
        if all(built == version for _t, version, _r, built in rows):
            text = "The toolchain for %s is built and up to date." % target.title
            build = "Build it again"
        else:
            text = "Build the toolchain for %s?" % target.title
            build = "Build it"
        lines = ["%s %s (from %s)%s" % (title, version, repo,
                                        "" if built is None else
                                        ", built now" if built == version else
                                        ", %s built now" % built)
                 for title, version, repo, built in rows]
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=text)
        dialog.format_secondary_text(
            "Cross compiler for %s, the versions %s's profile allows:\n\n%s\n\n"
            "crossdev builds it as a job; it takes an hour or more." % (
                resolved["tuple"], target.title, "\n".join(lines)))
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, build, Gtk.ResponseType.OK)
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return False
        from porthole.updater.elevate import run_as_root, service

        def done(ok, output):
            if not ok:
                self.error("The toolchain build wasn't queued", output)
        run_as_root(service("plugin", "crossdev", "toolchain", target.name), done)
        return False

    def build_system(self):
        """Work the build out as the user (a minute), then confirm"""
        from porthole.plugins.crossdev import roots
        target = roots.made_here().get(self.selected())
        if target is None:
            return
        self.build_button.set_sensitive(False)
        self.build_button.set_label("Working out the build…")

        def work():
            from porthole.plugins.crossdev import build
            try:
                plan = build.plan(target)
                GLib.idle_add(self.confirm_build, target, plan, None)
            except Exception as e:
                GLib.idle_add(self.confirm_build, target, None, "%s: %s" % (
                    type(e).__name__, e))
        threading.Thread(target=work, daemon=True).start()

    def confirm_build(self, target, plan, problem):
        self.build_button.set_label("Build the system…")
        self.update_buttons()
        if problem:
            self.error("Porthole couldn't work out the build", problem)
            return False
        BuildDialog(self, target, plan)
        return False

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
    """The computer it is for, a short name, its repositories (gentoo, and
    only what the user ticks), then architecture and profile from those"""

    def __init__(self, parent):
        super().__init__(title="New system", transient_for=parent, modal=True)
        from porthole.plugins.crossdev import roots
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Make it", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.repos = roots.host_repos()
        self.profiles = roots.profiles(self.repos)
        self.known = set(self.repos)
        self.ticked = set()
        grid = Gtk.Grid(row_spacing=8, column_spacing=12, border_width=12)
        self.get_content_area().add(grid)

        def row(n, text, widget, yalign=0.5):
            grid.attach(Gtk.Label(label=text, xalign=1, yalign=yalign), 0, n, 1, 1)
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

        repo_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.repo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        repo_area.pack_start(self.repo_box, False, False, 0)
        hint_row = Gtk.Box(spacing=8)
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
        hint.set_markup("<small>Tick what this system uses. A board's own repository "
                        "(like the Nintendo Switch overlay) brings its profiles; this "
                        "computer doesn't use the ones added here.</small>")
        hint_row.pack_start(hint, True, True, 0)
        add_repo = Gtk.Button(label="Add a repository…", valign=Gtk.Align.CENTER)
        add_repo.connect("clicked", self.add_repository)
        hint_row.pack_end(add_repo, False, False, 0)
        repo_area.pack_start(hint_row, False, False, 0)
        self.waiting = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        repo_area.pack_start(self.waiting, False, False, 0)
        row(2, "Repositories:", repo_area, yalign=0)
        self.watch = None
        self.connect("destroy", lambda d: self.watch and GLib.source_remove(self.watch))

        self.arch = Gtk.ComboBoxText()
        self.arch.connect("changed", lambda c: self.fill_profiles())
        row(3, "Architecture:", self.arch)
        self.profile = Gtk.ComboBoxText()
        self.profile.connect("changed", lambda c: self.validate())
        row(4, "Profile:", self.profile)
        self.experimental = Gtk.CheckButton(label="Show experimental profiles")
        self.experimental.connect("toggled", lambda b: self.fill_profiles())
        grid.attach(self.experimental, 1, 5, 1, 1)
        self.keep_binpkgs = Gtk.CheckButton(
            label="Keep binary packages of what is built for it")
        self.keep_binpkgs.set_tooltip_text(
            "To install them on the computer it is for, or host them for it and others. "
            "Without: just the system. You can change it later.")
        grid.attach(self.keep_binpkgs, 1, 6, 1, 1)
        self.message = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        grid.attach(self.message, 1, 7, 1, 1)
        self.fill_repos()
        self.fill_arches()
        self.show_all()

    def usable_profiles(self):
        """Profiles from gentoo and the ticked repositories"""
        return [p for p in self.profiles if p[0] == "gentoo" or p[0] in self.ticked]

    def add_repository(self, button):
        """By address or a folder, to Porthole's list for other systems;
        ticked for this one (the user asked for it), its profiles appear
        once it is downloaded"""
        from porthole.updater.repospage import AddByAddressDialog, AddFolderDialog
        menu = Gtk.Menu()
        for label, dialog in (("By address…", AddByAddressDialog),
                              ("A folder on this computer…", AddFolderDialog)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda i, d=dialog: d(
                self, set(self.repos), self.repository_added, ["--systems"]))
            menu.append(item)
        menu.show_all()
        menu.popup_at_widget(button, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def repository_added(self, ok, output):
        if not ok:
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                       message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE,
                                       text="The repository wasn't added")
            dialog.format_secondary_text(output or "Cancelled.")
            dialog.run()
            dialog.destroy()
            return
        self.reload_profiles()
        if self.watch is None:
            self.watch = GLib.timeout_add_seconds(5, self.reload_profiles)

    def reload_profiles(self):
        """Read the repositories and their profiles again (one added here
        is ticked; one renamed to its own name after downloading keeps its
        tick); keep looking while one is still being downloaded"""
        import os
        from porthole.plugins.crossdev import roots
        from porthole.updater import repopool
        self.repos = roots.host_repos()
        self.profiles = roots.profiles(self.repos)
        self.ticked |= set(self.repos) - self.known
        self.ticked &= set(self.repos)
        self.known = set(self.repos)
        pending = sorted(n for n, r in repopool.repos().items() if r["sync_type"] and
                         not os.path.isfile(os.path.join(r["location"], "profiles",
                                                         "repo_name")))
        self.fill_repos()
        self.fill_arches()
        self.waiting.set_markup("<small>%s</small>" % GLib.markup_escape_text(
            "Downloading %s; its profiles appear here when that's done." % ", ".join(pending)
            if pending else ""))
        if not pending:
            self.watch = None
            return False
        return True

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

    def fill_repos(self):
        for child in self.repo_box.get_children():
            child.destroy()
        gentoo = Gtk.CheckButton(label="gentoo", active=True, sensitive=False)
        gentoo.set_tooltip_text("Every system uses Gentoo's own repository")
        self.repo_box.pack_start(gentoo, False, False, 0)
        for name in sorted(self.repos):
            if name == "gentoo":
                continue
            check = Gtk.CheckButton(label=name, active=name in self.ticked)
            check.set_tooltip_text(self.repos[name])
            check.connect("toggled", self.repo_toggled)
            self.repo_box.pack_start(check, False, False, 0)
        self.repo_box.show_all()

    def repo_toggled(self, check):
        name = check.get_label()
        (self.ticked.add if check.get_active() else self.ticked.discard)(name)
        self.fill_arches()
        # ticking a board's repository: its profile is what the user is after
        theirs = [p for p in self.usable_profiles() if p[0] == name and p[3] != "exp"]
        if check.get_active() and theirs:
            self.arch.set_active_id(theirs[0][1])
            self.profile.set_active_id("%s:%s" % (theirs[0][0], theirs[0][2]))

    def fill_arches(self):
        """The architectures of the usable profiles (keeping the choice
        while it is still there; arm64 first, most boards are)"""
        from porthole.plugins.crossdev import roots
        chosen = self.arch.get_active_id()
        profile = self.profile.get_active_id()
        arches = sorted({p[1] for p in self.usable_profiles()},
                        key=lambda a: (a not in ARCHES, ARCHES.index(a) if a in ARCHES else 0, a))
        mine = roots.host_arch()
        self.arch.remove_all()
        for a in arches:
            self.arch.append(a, "%s (this computer's)" % a if a == mine else a)
        self.arch.set_active_id(chosen if chosen in arches else
                                "arm64" if "arm64" in arches else (arches or [None])[0])
        if profile:
            self.profile.set_active_id(profile)

    def fill_profiles(self):
        arch = self.arch.get_active_id()
        show_exp = self.experimental.get_active()
        self.profile.remove_all()
        for repo, a, name, status in self.usable_profiles():
            if a == arch and (show_exp or status != "exp"):
                label = "%s: %s%s" % (repo, name, "" if status == "stable" else
                                      " (%s)" % status)
                self.profile.append("%s:%s" % (repo, name), label)
        self.profile.set_active(0)
        self.validate()

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
        for name in sorted(self.ticked):
            args += ["--repo", name]
        if self.keep_binpkgs.get_active():
            args.append("--keep-binpkgs")
        return args


class BuildDialog(Gtk.Dialog):
    """What building a system installs, and the changes it needs (this
    computer's only with the user's tick)"""

    def __init__(self, parent, target, plan):
        super().__init__(title="Porthole: build %s" % target.title, transient_for=parent,
                         modal=True)
        self.set_default_size(720, 560)
        self.parent_window, self.target, self.plan = parent, target, plan
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)

        def small(text):
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=90)
            label.set_markup("<small>%s</small>" % GLib.markup_escape_text(text))
            return label

        def heading(text):
            label = Gtk.Label(xalign=0, margin_top=6)
            label.set_markup("<b>%s</b>" % GLib.markup_escape_text(text))
            return label

        def listing(lines, height=90):
            view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
            view.get_buffer().set_text("\n".join(lines))
            scroller = Gtk.ScrolledWindow(min_content_height=height)
            scroller.add(view)
            return scroller
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(box)
        area.pack_start(scroller, True, True, 0)
        if not plan["ok"]:
            box.pack_start(heading("Portage can't work this build out"), False, False, 0)
            box.pack_start(small("\n".join(plan["other"]) or "See its output below."),
                           False, False, 0)
            box.pack_start(listing(plan["output"].splitlines()[-60:], 260), True, True, 0)
            self.add_button("Close", Gtk.ResponseType.CANCEL)
            self.show_all()
            self.run()
            self.destroy()
            return
        box.pack_start(heading("%d packages for %s (%s)" % (
            len(plan["root"]), target.title, plan["chost"])), False, False, 0)
        box.pack_start(small(
            "Cross-compiled here, in order: kernel headers and C library, the base system, "
            "the system's package list, then its first-boot setup. Hours, for a whole "
            "system; it can be stopped and started again, and picks up where it stopped."),
            False, False, 0)
        box.pack_start(listing(plan["root"], 110), False, False, 0)
        changes = plan["root_use"] + plan["root_keywords"]
        if changes:
            box.pack_start(heading("Changes to %s's settings" % target.title), False, False, 0)
            box.pack_start(small("Its packages need these; they go in its own "
                                 "package.use / package.accept_keywords (00-porthole-build)."),
                           False, False, 0)
            box.pack_start(listing(changes, 70), False, False, 0)
        self.allow = None
        if plan["host"]:
            box.pack_start(heading("On this computer: %d build tools" % len(plan["host"])),
                           False, False, 0)
            box.pack_start(small(
                "Some packages run tools while they build, and those run here, so they are "
                "installed or rebuilt on this computer, as in any cross build:"),
                False, False, 0)
            box.pack_start(listing(plan["host"], 70), False, False, 0)
        if plan["host_use"]:
            box.pack_start(heading("Changes to this computer's settings"), False, False, 0)
            box.pack_start(small(
                "Those tools need these USE changes here (often one more Python version: "
                "the system's Python is another than this computer's). Porthole keeps them "
                "in a file of its own, /etc/portage/package.use/porthole-cross-%s, removed "
                "with the system; nothing else on this computer changes." % target.name),
                False, False, 0)
            box.pack_start(listing(plan["host_use"], 90), False, False, 0)
            self.allow = Gtk.CheckButton(
                label="Allow these changes on this computer, and the same kind later in "
                      "this build (another Python version for build tools that run here)")
            self.allow.connect("toggled", lambda b: self.set_response_sensitive(
                Gtk.ResponseType.OK, b.get_active()))
            box.pack_start(self.allow, False, False, 0)
        self.allow_python = None
        if not plan["host_use"]:
            self.allow_python = Gtk.CheckButton(
                label="If the build needs another Python version for build tools that run "
                      "here, add it without asking (in porthole-cross-%s)" % target.name)
            box.pack_start(self.allow_python, False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Build it", Gtk.ResponseType.OK)
        if self.allow is not None:
            self.set_response_sensitive(Gtk.ResponseType.OK, False)
        self.show_all()
        answer = self.run()
        self.python_ok = bool(self.allow_python and self.allow_python.get_active())
        self.destroy()
        if answer == Gtk.ResponseType.OK:
            self.queue()

    def queue(self):
        import json
        from porthole.updater.elevate import run_as_root, service
        changes = {"host_use": self.plan["host_use"], "root_use": self.plan["root_use"],
                   "root_keywords": self.plan["root_keywords"],
                   "allow_python": self.allow is not None or self.python_ok}

        def done(ok, output):
            if not ok:
                self.parent_window.error("The build wasn't queued", output)
        run_as_root(service("plugin", "crossdev", "build", self.target.name,
                            json.dumps(changes)), done)
