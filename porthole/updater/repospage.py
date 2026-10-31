"""
    Porthole: Settings > Repositories

    The repositories this computer uses, how many installed packages came
    from each, and adding (from Gentoo's list, by address, or a folder
    already here), removing and syncing them. Changes are made as root by
    porthole-update-service (`repo add|remove|sync|use|drop`), which
    checks them.

    Once there are systems for other computers, "Repositories of" switches
    to one of them: it uses this computer's repositories or ones from
    Porthole's list for other systems only (repopool.py), which this
    computer never uses.

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

from porthole.updater import system
from porthole.updater.elevate import run_as_root, service


def _small(text, **props):
    label = Gtk.Label(xalign=0, wrap=True, max_width_chars=90, **props)
    label.set_markup("<small>%s</small>" % GLib.markup_escape_text(text))
    return label


def _error(parent, title, text):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True,
                               message_type=Gtk.MessageType.ERROR,
                               buttons=Gtk.ButtonsType.CLOSE, text=title)
    dialog.format_secondary_text(text)
    dialog.run()
    dialog.destroy()


def _follow(output):
    """Show a queued job (the last word of the service's output) in the
    job window"""
    from porthole.updater.follow import show_in_terminal
    words = (output or "").split()
    if words:
        show_in_terminal(words[-1])


class RepositoriesPage(Gtk.Box):
    """on_changed(): portage must read its configuration again"""

    def __init__(self, on_changed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=12)
        self.on_changed = on_changed
        self.target = None  # None: this computer, else a system's name
        self.pack_start(_small(
            "Where packages come from. Gentoo's own repository comes first; the others "
            "add packages (or newer versions). Changing these asks for administrator "
            "rights."), False, False, 0)
        self.target_row = Gtk.Box(spacing=8, no_show_all=True)
        self.target_row.pack_start(Gtk.Label(label="Repositories of:"), False, False, 0)
        self.target_combo = Gtk.ComboBoxText()
        self.target_combo.connect("changed", self.target_changed)
        self.target_row.pack_start(self.target_combo, False, False, 0)
        self.target_note = _small("")
        self.target_row.pack_start(self.target_note, True, True, 0)
        self.pack_start(self.target_row, False, False, 0)
        # name, syncs with, installed / where, defined in, is main, used, can untick
        self.store = Gtk.ListStore(str, str, str, str, bool, bool, bool)
        self.view = Gtk.TreeView(model=self.store)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.use_toggled)
        self.use_column = Gtk.TreeViewColumn("Use", toggle, active=5, activatable=6)
        self.view.append_column(self.use_column)
        self.columns = []
        for i, title in enumerate(("Repository", "Syncs with", "Installed from it",
                                   "Defined in")):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            column.set_resizable(True)
            self.view.append_column(column)
            self.columns.append(column)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)
        buttons = Gtk.Box(spacing=6)
        for name, label, callback in (("list", "Add from Gentoo's list...", self.add_from_list),
                                      ("address", "Add by address...", self.add_by_address),
                                      ("folder", "Add a folder...", self.add_folder),
                                      ("remove", "Remove...", self.remove),
                                      ("move", "For other systems only...", self.move),
                                      ("sync", "Sync now", self.sync)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda b, c=callback: c())
            buttons.pack_start(button, False, False, 0)
            setattr(self, "button_" + name, button)
        self.pack_start(buttons, False, False, 0)
        self.view.get_selection().connect("changed", self.selected)
        self.repos = []
        self.refresh()

    def systems(self):
        """{name: Target} of the other systems"""
        from porthole.updater import targets
        return {n: t for n, t in targets.load().items() if not t.local}

    def fill_targets(self):
        systems = self.systems()
        self.target_combo.handler_block_by_func(self.target_changed)
        self.target_combo.remove_all()
        self.target_combo.append("", "This computer")
        for name, target in sorted(systems.items()):
            self.target_combo.append(name, target.title)
        if self.target not in systems:
            self.target = None
        self.target_combo.set_active_id(self.target or "")
        self.target_combo.handler_unblock_by_func(self.target_changed)
        if systems:
            for child in self.target_row.get_children():
                child.show()
        self.target_row.set_visible(bool(systems))

    def target_changed(self, combo):
        self.target = combo.get_active_id() or None
        self.refresh(targets_too=False)

    def where(self):
        """Service arguments saying whose repository an addition is"""
        return ["--target", self.target] if self.target else []

    def refresh(self, targets_too=True):
        if targets_too:
            self.fill_targets()
        self.store.clear()
        local = self.target is None
        self.use_column.set_visible(not local)
        self.columns[2].set_title("Installed from it" if local else "On this computer")
        self.columns[3].set_visible(local)
        if local:
            self.target_note.set_markup("")
            self.repos = system.repositories()
            for r in self.repos:
                syncs = ("%s %s" % (r["sync_type"], r["sync_uri"])).strip() or "(not synced)"
                if r.get("missing"):
                    syncs += " (not downloaded yet)" if r["sync_type"] else " (folder missing)"
                self.store.append([r["name"], syncs, str(r["installed"]),
                                   r["file"] or "(portage's default)", r["main"], True, False])
        else:
            self.repos = self.system_repos(self.target)
            self.target_note.set_markup(
                "<small>Tick what it uses. Repositories added here are only for the "
                "other systems: this computer doesn't use them.</small>")
            for r in self.repos:
                syncs = ("%s %s" % (r["sync_type"], r["sync_uri"])).strip() or "(not synced)"
                self.store.append([r["name"], syncs, r["where"], "", r["main"], r["used"],
                                   not r["main"]])
        self.selected()

    def system_repos(self, name):
        """What a system can use, as rows like system.repositories()'s"""
        from porthole.updater import repopool
        from porthole.updater.runner import roots_repos
        target = self.systems()[name]
        used = set(repopool.target_repos(target.portage_dir))
        pool = repopool.repos()
        mine = {r["name"]: r for r in system.repositories()}
        rows = []
        for repo, location in sorted(roots_repos().items(),
                                     key=lambda kv: (kv[0] != "gentoo", kv[0])):
            info = pool.get(repo) or mine.get(repo) or {}
            rows.append({"name": repo, "location": location, "main": repo == "gentoo",
                         "used": repo in used or repo == "gentoo", "pool": repo in pool,
                         "sync_type": info.get("sync_type", ""),
                         "sync_uri": info.get("sync_uri", ""), "file": None,
                         "where": "for other systems only" if repo in pool else
                         "this computer's"})
        return rows

    def use_toggled(self, renderer, path):
        row = self.store[path]
        name, used = row[0], row[5]
        target = self.target

        def done(ok, output):
            if not ok:
                _error(self.get_toplevel(), "That wasn't changed", output or "Cancelled.")
            self.refresh()
        run_as_root(service("repo", "drop" if used else "use", name, "--target", target),
                    done)

    def chosen(self):
        model, row = self.view.get_selection().get_selected()
        name = model[row][0] if row else None
        return next((r for r in self.repos if r["name"] == name), None)

    def selected(self, *args):
        repo = self.chosen()
        # a system's: only the list's repositories are removed from here
        self.button_remove.set_sensitive(bool(repo) and not repo["main"] and (
            self.target is None or repo.get("pool", False)))
        self.button_move.set_visible(self.target is None)
        self.button_move.set_sensitive(bool(repo) and not repo["main"] and bool(repo["file"]))
        self.button_sync.set_sensitive(bool(repo) and bool(repo["sync_type"]))

    def changed(self):
        if self.on_changed:
            self.on_changed()
        self.refresh()

    # -- adding --

    def add_from_list(self):
        AddFromListDialog(self.get_toplevel(), {r["name"] for r in self.repos}, self.added,
                          self.where())

    def add_by_address(self):
        AddByAddressDialog(self.get_toplevel(), {r["name"] for r in self.repos}, self.added,
                           self.where())

    def add_folder(self):
        AddFolderDialog(self.get_toplevel(), {r["name"] for r in self.repos}, self.added,
                        self.where())

    def added(self, ok, output):
        if not ok:
            _error(self.get_toplevel(), "The repository wasn't added", output or "Cancelled.")
            return
        if (output or "").split()[-1:] != ["added"]:
            _follow(output)  # its first sync
        self.changed()

    # -- removing --

    def remove(self):
        repo = self.chosen()
        if repo and repo.get("pool"):
            RemoveFromListDialog(self.get_toplevel(), repo, self.changed)
        elif repo:
            RemoveDialog(self.get_toplevel(), repo, self.changed)

    def move(self):
        repo = self.chosen()
        if not repo:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(), modal=True,
            message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
            text="Use %s only for other systems?" % repo["name"])
        dialog.format_secondary_text(
            "This computer stops using it (%s is deleted); it goes on Porthole's list for "
            "the systems you build for other computers, where you tick which use it. Its "
            "files stay in %s.%s" % (
                repo["file"], repo["location"],
                "\n\n%d installed package%s came from it; they get no more updates from it."
                % (repo["installed"], "" if repo["installed"] == 1 else "s")
                if repo["installed"] else ""))
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Move it", Gtk.ResponseType.OK)
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return

        def done(ok, output):
            if not ok:
                _error(self.get_toplevel(), "%s wasn't moved" % repo["name"],
                       output or "Cancelled.")
            self.changed()
        run_as_root(service("repo", "move", repo["name"], "--systems"), done)

    def sync(self):
        repo = self.chosen()
        if not repo:
            return

        def done(ok, output):
            if not ok:
                _error(self.get_toplevel(), "Couldn't sync %s" % repo["name"],
                       output or "Cancelled.")
                return
            _follow(output)
        run_as_root(service("repo", "sync", repo["name"]), done)


class AddFromListDialog(Gtk.Dialog):
    """Gentoo's list of repositories (as eselect repository offers them)"""

    def __init__(self, parent, existing, on_done, where=()):
        super().__init__(title="Porthole: add a repository", transient_for=parent, modal=True)
        self.existing, self.on_done, self.where = existing, on_done, list(where)
        self.set_default_size(720, 520)
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        area.pack_start(_small(
            "Repositories listed by Gentoo. Official ones are run by Gentoo developers; "
            "the rest by their owners, and Gentoo doesn't check what they contain: add "
            "only ones you trust."), False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text="Search names and descriptions")
        self.search.connect("search-changed", lambda e: self.fill())
        area.pack_start(self.search, False, False, 0)
        self.store = Gtk.ListStore(str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, title in enumerate(("Repository", "Kind", "Description")):
            renderer = Gtk.CellRendererText()
            if i == 2:
                renderer.set_property("ellipsize", 3)  # end
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            column.set_resizable(True)
            column.set_expand(i == 2)
            self.view.append_column(column)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        area.pack_start(scroller, True, True, 0)
        self.detail = _small("Loading Gentoo's list...")
        area.pack_start(self.detail, False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.ok = self.add_button("Add", Gtk.ResponseType.OK)
        self.ok.set_sensitive(False)
        self.view.get_selection().connect("changed", self.selected)
        self.connect("response", self.on_response)
        self.repos = []
        self.show_all()
        threading.Thread(target=self.load, daemon=True).start()

    def load(self):
        try:
            repos, error = system.official_repos(), None
        except Exception as e:
            repos, error = [], "Couldn't get Gentoo's list: %s" % e
        GLib.idle_add(self.loaded, repos, error)

    def loaded(self, repos, error):
        self.repos = repos
        self.detail.set_markup("<small>%s</small>" % GLib.markup_escape_text(
            error or "%d repositories." % len(repos)))
        self.fill()
        return False

    def fill(self):
        words = self.search.get_text().lower().split()
        self.store.clear()
        for r in self.repos:
            text = (r["name"] + " " + r["description"]).lower()
            if all(w in text for w in words):
                kind = "official" if r["status"] == "official" else "community"
                if r["name"] in self.existing:
                    kind += ", added"
                self.store.append([r["name"], kind, r["description"]])

    def chosen(self):
        model, row = self.view.get_selection().get_selected()
        name = model[row][0] if row else None
        return next((r for r in self.repos if r["name"] == name), None)

    def source(self, repo):
        """The address to sync from: https git first (no keys needed)"""
        for kind, url in repo["sources"]:
            if kind == "git" and url.startswith("https://"):
                return kind, url
        for kind, url in repo["sources"]:
            if kind in system.SYNC_TYPES:
                return kind, url
        return None, None

    def selected(self, *args):
        repo = self.chosen()
        kind, url = self.source(repo) if repo else (None, None)
        self.ok.set_sensitive(bool(url) and repo["name"] not in self.existing)
        if repo:
            self.detail.set_markup("<small><b>%s</b> (%s, %s quality)\n%s\n%s</small>" % tuple(
                GLib.markup_escape_text(x) for x in (
                    repo["name"], repo["status"] or "?", repo["quality"] or "?",
                    repo["description"], ("Syncs with %s %s" % (kind, url)) if url else
                    "No address Porthole can sync from.")))

    def on_response(self, dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        repo = self.chosen()
        kind, url = self.source(repo)
        self.set_sensitive(False)

        def done(ok, output):
            self.destroy()
            self.on_done(ok, output)
        run_as_root(service("repo", "add", repo["name"], "--sync-type", kind, "--uri", url,
                            *self.where), done)


class AddByAddressDialog(Gtk.Dialog):
    def __init__(self, parent, existing, on_done, where=()):
        super().__init__(title="Porthole: add a repository", transient_for=parent, modal=True)
        self.existing, self.on_done, self.where = existing, on_done, list(where)
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        area.pack_start(_small(
            "A repository that isn't in Gentoo's list (your own, or one you were given "
            "the address of). Add only repositories you trust: their packages are built "
            "and installed as root."), False, False, 0)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        self.name = Gtk.Entry(placeholder_text="e.g. switch", hexpand=True)
        self.kind = Gtk.ComboBoxText()
        for kind in system.SYNC_TYPES:
            self.kind.append(kind, kind)
        self.kind.set_active_id("git")
        self.uri = Gtk.Entry(placeholder_text="https://github.com/...", hexpand=True)
        for row, (label, widget) in enumerate((("Name", self.name), ("Sync with", self.kind),
                                               ("Address", self.uri))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        area.pack_start(grid, False, False, 0)
        self.note = _small("")
        area.pack_start(self.note, False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Add", Gtk.ResponseType.OK)
        self.connect("response", self.on_response)
        self.show_all()

    def on_response(self, dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        name, uri = self.name.get_text().strip(), self.uri.get_text().strip()
        problem = None
        if not system._REPO_NAME.match(name):
            problem = "The name may use letters, digits, - and _."
        elif name in self.existing:
            problem = "There already is a repository called %s." % name
        else:
            try:
                system.check_repo_uri(uri)
            except ValueError as e:
                problem = str(e)
        if problem:
            self.note.set_markup("<small><b>%s</b></small>" % GLib.markup_escape_text(problem))
            return
        self.set_sensitive(False)

        def done(ok, output):
            self.destroy()
            self.on_done(ok, output)
        run_as_root(service("repo", "add", name, "--sync-type", self.kind.get_active_id(),
                            "--uri", uri, *self.where), done)


class AddFolderDialog(Gtk.Dialog):
    """A repository already on this computer (the user's own checkout):
    used where it is, never synced or deleted by Porthole"""

    def __init__(self, parent, existing, on_done, where=()):
        super().__init__(title="Porthole: add a folder", transient_for=parent, modal=True)
        self.existing, self.on_done, self.where = existing, on_done, list(where)
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        area.pack_start(_small(
            "A repository you keep yourself, like your own checkout of an overlay you "
            "work on. Porthole uses it where it is and never syncs or deletes it. Its "
            "packages are built as root: keep it where only you can change it."),
            False, False, 0)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        self.folder = Gtk.FileChooserButton(title="The repository's folder",
                                            action=Gtk.FileChooserAction.SELECT_FOLDER,
                                            hexpand=True)
        self.folder.connect("file-set", self.folder_set)
        self.name = Gtk.Entry(placeholder_text="from its profiles/repo_name", hexpand=True)
        for row, (label, widget) in enumerate((("Folder", self.folder), ("Name", self.name))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        area.pack_start(grid, False, False, 0)
        self.note = _small("")
        area.pack_start(self.note, False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Add", Gtk.ResponseType.OK)
        self.connect("response", self.on_response)
        self.show_all()

    def folder_set(self, button):
        import os
        path = button.get_filename() or ""
        try:
            with open(os.path.join(path, "profiles", "repo_name")) as f:
                self.name.set_text(f.read().strip())
            self.note.set_markup("")
        except OSError:
            self.note.set_markup("<small>No profiles/repo_name there: is it a "
                                 "repository?</small>")

    def on_response(self, dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        name, path = self.name.get_text().strip(), self.folder.get_filename() or ""
        problem = None
        if not path:
            problem = "Choose the folder."
        elif not system._REPO_NAME.match(name):
            problem = "The name may use letters, digits, - and _."
        elif name in self.existing:
            problem = "There already is a repository called %s." % name
        if problem:
            self.note.set_markup("<small><b>%s</b></small>" % GLib.markup_escape_text(problem))
            return
        self.set_sensitive(False)

        def done(ok, output):
            self.destroy()
            self.on_done(ok, output)
        run_as_root(service("repo", "add", name, "--folder", path, *self.where), done)


class RemoveFromListDialog(Gtk.Dialog):
    """Removing one of the repositories for other systems: every system
    stops using it"""

    def __init__(self, parent, repo, on_done):
        super().__init__(title="Porthole: remove %s" % repo["name"], transient_for=parent,
                         modal=True)
        from porthole.updater import repopool, targets
        from porthole.updater.runner import deletable
        self.repo, self.on_done = repo, on_done
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        users = [t.title for t in targets.load().values()
                 if not t.local and repo["name"] in repopool.target_repos(t.portage_dir)]
        area.pack_start(_small(
            ("Used by %s, which stop using it." % ", ".join(users)) if users else
            "No system uses it."), False, False, 0)
        self.delete_files = Gtk.CheckButton(label="Also delete its files (%s)" % repo["location"])
        if repo["sync_type"] and deletable(repo["location"]):
            self.delete_files.set_active(True)
            area.pack_start(self.delete_files, False, False, 0)
        else:
            area.pack_start(_small("Its files (%s) are kept." % repo["location"]),
                            False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Remove", Gtk.ResponseType.OK)
        self.connect("response", self.on_response)
        self.show_all()

    def on_response(self, dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        self.set_sensitive(False)
        args = ["repo", "remove", self.repo["name"], "--systems"] + (
            ["--delete-files"] if self.delete_files.get_active() else [])

        def removed(ok, output):
            if not ok:
                self.set_sensitive(True)
                _error(self, "%s wasn't removed" % self.repo["name"], output or "Cancelled.")
                return
            self.destroy()
            self.on_done()
        run_as_root(service(*args), removed)


class RemoveDialog(Gtk.Dialog):
    """Removing a repository: what happens to the packages installed from it"""

    def __init__(self, parent, repo, on_done):
        super().__init__(title="Porthole: remove %s" % repo["name"], transient_for=parent,
                         modal=True)
        self.repo, self.on_done = repo, on_done
        self.set_default_size(560, -1)
        packages = system.installed_from(repo["name"])
        self.packages = packages
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        if packages:
            area.pack_start(_small(
                "%d installed package%s came from %s:" % (
                    len(packages), "" if len(packages) == 1 else "s", repo["name"])),
                False, False, 0)
            listing = Gtk.TextView(editable=False, cursor_visible=False,
                                   wrap_mode=Gtk.WrapMode.WORD)
            listing.get_buffer().set_text(", ".join(packages))
            scroller = Gtk.ScrolledWindow(min_content_height=80, max_content_height=160,
                                          propagate_natural_height=True)
            scroller.add(listing)
            area.pack_start(scroller, False, False, 0)
            self.keep = Gtk.RadioButton.new_with_label(
                None, "Keep them installed (they get no more updates; Updates offers to "
                      "replace or remove them when something needs that)")
            self.drop = Gtk.RadioButton.new_with_label_from_widget(
                self.keep, "Remove them too (queued as a job)")
            for widget in (self.keep, self.drop):
                widget.get_child().set_line_wrap(True)
                area.pack_start(widget, False, False, 0)
        else:
            area.pack_start(_small("No installed package came from %s." % repo["name"]),
                            False, False, 0)
            self.drop = None
        self.delete_files = Gtk.CheckButton(label="Also delete its files (%s)" % repo["location"])
        from porthole.updater.runner import deletable
        if deletable(repo["location"]):
            self.delete_files.set_active(True)
            area.pack_start(self.delete_files, False, False, 0)
        else:
            area.pack_start(_small("Its files (%s) are kept: Porthole only deletes "
                                   "checkouts in /var/db/repos." % repo["location"]),
                            False, False, 0)
        if repo["file"]:
            area.pack_start(_small("Its definition, %s, is deleted." % repo["file"]),
                            False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Remove", Gtk.ResponseType.OK)
        self.connect("response", self.on_response)
        self.show_all()

    def on_response(self, dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        self.set_sensitive(False)
        name = self.repo["name"]
        args = ["repo", "remove", name] + (["--delete-files"]
                                          if self.delete_files.get_active() else [])

        def removed(ok, output):
            if not ok:
                self.set_sensitive(True)
                _error(self, "%s wasn't removed" % name, output or "Cancelled.")
                return
            if self.drop is not None and self.drop.get_active():
                # unmerging needs only what is installed, not the repository
                run_as_root(service("submit", "emerge", "--", "emerge", "--unmerge",
                                    *["=" + p for p in self.packages]),
                            lambda ok2, out2: (_follow(out2) if ok2 else _error(
                                self, "The packages weren't removed", out2 or "Cancelled.")))
            self.destroy()
            self.on_done()
        run_as_root(service(*args), removed)
