"""
    Porthole: Settings > Binary packages

    Using binary packages (from binhosts in binrepos.conf, when their USE
    matches) and building, keeping and hosting them. make.conf is left
    alone: the switches are in updater.conf and Porthole adds --getbinpkg
    and --buildpkg to the emerges it runs. Changes go through
    porthole-update-service as root.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import socket
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole.updater import system
from porthole.updater.elevate import run_as_root, service
from porthole.updater.repospage import _error, _follow, _small
from porthole.updater.service import load_config


def _heading(text):
    label = Gtk.Label(xalign=0, margin_top=6)
    label.set_markup("<b>%s</b>" % GLib.markup_escape_text(text))
    return label


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "bytes" else "%.1f %s" % (n, unit)
        n /= 1024.0


class BinaryPackagesPage(Gtk.Notebook):
    def __init__(self):
        super().__init__(border_width=6)
        self.saved = load_config()
        self.use_tab = self._tab("Using binary packages")
        self.build_tab = self._tab("Building and hosting")
        self.fill_use()
        self.fill_build()

    def _tab(self, title):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=12,
                      valign=Gtk.Align.START)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(box)
        self.append_page(scroller, Gtk.Label(label=title))
        return box

    # -- using them --

    def fill_use(self):
        box = self.use_tab
        for child in box.get_children():
            child.destroy()
        box.pack_start(_small(
            "A binary package is a package someone already built, downloaded instead of "
            "compiled. Portage uses one only if it was built with the same USE flags as "
            "yours; everything else is still built here."), False, False, 0)
        self.use_check = Gtk.CheckButton(
            label="Use binary packages when one matches (emerge --getbinpkg)")
        self.use_check.set_active(self.saved["use_binpkgs"])
        box.pack_start(self.use_check, False, False, 0)

        box.pack_start(_heading("Binhosts"), False, False, 0)
        level = system.cpu_level()
        if level:
            box.pack_start(_small("This CPU runs x86-64-v%d binaries (and older levels)."
                                  % level), False, False, 0)
        self.binhosts = system.binrepos()
        self.store = Gtk.ListStore(str, str, str, str)
        for b in self.binhosts:
            needs = system.binhost_level(b["uri"])
            fits = ("?" if not needs or not level else
                    "yes" if needs <= level else "NO: needs x86-64-v%d" % needs)
            self.store.append([b["name"], b["uri"], fits,
                               "yes" if b["verify"] else "no"])
        self.view = Gtk.TreeView(model=self.store)
        for i, title in enumerate(("Name", "Address", "Runs on this CPU", "Signatures checked")):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            column.set_resizable(True)
            self.view.append_column(column)
        box.pack_start(self.view, False, False, 0)
        buttons = Gtk.Box(spacing=6)
        add = Gtk.Button(label="Add a binhost...")
        add.connect("clicked", lambda b: AddBinhostDialog(self.get_toplevel(),
                                                          {x["name"] for x in self.binhosts},
                                                          self.changed))
        remove = Gtk.Button(label="Remove")
        remove.connect("clicked", lambda b: self.remove_binhost())
        buttons.pack_start(add, False, False, 0)
        buttons.pack_start(remove, False, False, 0)
        box.pack_start(buttons, False, False, 0)

        box.pack_start(_heading("What you would get"), False, False, 0)
        conf = system.read_make_conf()
        flags = (conf["vars"].get("COMMON_FLAGS") or conf["vars"].get("CFLAGS") or
                 {}).get("text", "")
        march = next((w.split("=", 1)[1] for w in flags.split() if w.startswith("-march=")),
                     None)
        if march and not march.startswith("x86-64"):
            box.pack_start(_small(
                "You build with -march=%s. Binary packages are built for a generic CPU "
                "level, so each one downloaded replaces a build tuned for this CPU with a "
                "generic one." % march), False, False, 0)
        self.match_label = _small("")
        check = Gtk.Button(label="Check how many of my packages have a matching build",
                           halign=Gtk.Align.START)
        check.connect("clicked", self.check_match)
        box.pack_start(check, False, False, 0)
        box.pack_start(self.match_label, False, False, 0)
        box.show_all()

    def check_match(self, button):
        if not self.binhosts:
            return
        uri = self.binhosts[0]["uri"]
        if not uri.startswith(("http://", "https://")):
            self.match_label.set_markup("<small>Only binhosts reached over http(s) can be "
                                        "checked here.</small>")
            return
        button.set_sensitive(False)
        self.match_label.set_markup("<small>Downloading %s's index (about 20 MB) and "
                                    "comparing...</small>" % GLib.markup_escape_text(
                                        self.binhosts[0]["name"]))

        def work():
            try:
                matching, missing, total = system.binhost_match(uri)
                text = ("%d of your %d packages (%d%%) have a build with your USE flags; "
                        "%d aren't on it at all; the rest would still be built here."
                        % (matching, total, 100 * matching // max(total, 1), missing))
            except Exception as e:
                text = "Couldn't check: %s" % e
            GLib.idle_add(done, text)

        def done(text):
            button.set_sensitive(True)
            self.match_label.set_markup("<small>%s</small>" % GLib.markup_escape_text(text))
            return False
        threading.Thread(target=work, daemon=True).start()

    def remove_binhost(self):
        model, row = self.view.get_selection().get_selected()
        if not row:
            return
        name = model[row][0]

        def done(ok, output):
            if not ok:
                _error(self.get_toplevel(), "%s wasn't removed" % name, output or "Cancelled.")
            self.changed()
        run_as_root(service("binhost", "remove", name), done)

    def changed(self, *args):
        self.fill_use()

    # -- building and hosting --

    def fill_build(self):
        box = self.build_tab
        box.pack_start(_small(
            "Keep a binary package of everything built here, to install again quickly or "
            "to share with other computers that use the same profile, USE flags and CPU "
            "level."), False, False, 0)
        self.build_check = Gtk.CheckButton(
            label="Keep binary packages of what this computer builds (emerge --buildpkg)")
        self.build_check.set_active(self.saved["build_binpkgs"])
        box.pack_start(self.build_check, False, False, 0)
        pkgdir, count, size = system.pkgdir_stats()
        box.pack_start(_heading("The package directory"), False, False, 0)
        box.pack_start(_small("%s: %d package%s, %s" % (pkgdir, count, "" if count == 1 else "s",
                                                         _size(size))), False, False, 0)
        jobs = Gtk.Box(spacing=6)
        import shutil
        for action, label, tip, available in (
                ("index", "Rebuild the index", "emaint binhost --fix: the Packages file "
                 "other computers read", True),
                ("clean", "Clean old packages", "eclean-pkg --deep: removes packages of "
                 "versions no longer in the tree", bool(shutil.which("eclean-pkg")))):
            button = Gtk.Button(label=label)
            button.set_tooltip_text(tip if available else
                                    "Needs eclean-pkg (app-portage/gentoolkit)")
            button.set_sensitive(available)
            button.connect("clicked", lambda b, a=action: self.job(a))
            jobs.pack_start(button, False, False, 0)
        box.pack_start(jobs, False, False, 0)

        box.pack_start(_heading("Uploading"), False, False, 0)
        box.pack_start(_small("Copy the package directory to a server (rsync, as root: "
                              "root's SSH key must be allowed there)."), False, False, 0)
        row = Gtk.Box(spacing=6)
        self.upload_entry = Gtk.Entry(text=self.saved["binpkg_upload"], hexpand=True,
                                      placeholder_text="user@server:/srv/binpkgs")
        upload = Gtk.Button(label="Upload now")
        upload.set_tooltip_text("Uses the destination as saved (press Apply first)")
        upload.connect("clicked", lambda b: self.job("upload"))
        row.pack_start(self.upload_entry, True, True, 0)
        row.pack_start(upload, False, False, 0)
        box.pack_start(row, False, False, 0)

        box.pack_start(_heading("Serving them from this computer"), False, False, 0)
        host = socket.gethostname()
        box.pack_start(_small(
            "Porthole doesn't run a web server. To share the directory, add this to one "
            "you run, then give other computers the address below as a binhost (Add a "
            "binhost; untick signature checking unless you sign packages)."), False, False, 0)
        snippet = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        snippet.get_buffer().set_text(
            "# nginx\nlocation /binpkgs/ {\n    alias %s/;\n    autoindex on;\n}\n\n"
            "# lighttpd\nalias.url += ( \"/binpkgs/\" => \"%s/\" )\n\n"
            "# address for the other computers\nhttp://%s/binpkgs" % (pkgdir, pkgdir, host))
        box.pack_start(snippet, False, False, 0)
        box.show_all()

    def job(self, action):
        def done(ok, output):
            if not ok:
                _error(self.get_toplevel(), "Couldn't start it", output or "Cancelled.")
                return
            _follow(output)
        run_as_root(service("binpkgs", action), done)

    # -- saving --

    def values(self):
        return {"use_binpkgs": self.use_check.get_active(),
                "build_binpkgs": self.build_check.get_active(),
                "binpkg_upload": self.upload_entry.get_text().strip()}

    def save(self, callback=None):
        values = self.values()
        if all(values[k] == self.saved[k] for k in values):
            if callback:
                callback(True, "")
            return
        if values["binpkg_upload"]:
            try:
                system.check_upload_destination(values["binpkg_upload"])
            except ValueError as e:
                if callback:
                    callback(False, str(e))
                return
        settings = ["use_binpkgs=%s" % ("yes" if values["use_binpkgs"] else "no"),
                    "build_binpkgs=%s" % ("yes" if values["build_binpkgs"] else "no"),
                    "binpkg_upload=%s" % values["binpkg_upload"]]

        def done(ok, output):
            if ok:
                self.saved.update(values)
            if callback:
                callback(ok, output)
        run_as_root(service("config", *settings), done)


class AddBinhostDialog(Gtk.Dialog):
    def __init__(self, parent, existing, on_done):
        super().__init__(title="Porthole: add a binhost", transient_for=parent, modal=True)
        self.existing, self.on_done = existing, on_done
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        area.pack_start(_small(
            "A binhost to download binary packages from: another of your computers, or "
            "a public one. Its packages are installed as root, so add only binhosts you "
            "trust."), False, False, 0)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        self.name = Gtk.Entry(placeholder_text="e.g. desktop", hexpand=True)
        self.uri = Gtk.Entry(placeholder_text="http://desktop/binpkgs", hexpand=True)
        grid.attach(Gtk.Label(label="Name", xalign=0), 0, 0, 1, 1)
        grid.attach(self.name, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Address", xalign=0), 0, 1, 1, 1)
        grid.attach(self.uri, 1, 1, 1, 1)
        area.pack_start(grid, False, False, 0)
        self.verify = Gtk.CheckButton(label="Its packages are signed: check the signatures")
        self.verify.set_active(True)
        area.pack_start(self.verify, False, False, 0)
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
        if not system.BINHOST_NAME.match(name):
            problem = "The name may use letters, digits, - and _."
        elif name in self.existing:
            problem = "There already is a binhost called %s." % name
        else:
            try:
                system.check_binhost_uri(uri)
            except ValueError as e:
                problem = str(e)
        if problem:
            self.note.set_markup("<small><b>%s</b></small>" % GLib.markup_escape_text(problem))
            return
        args = ["binhost", "add", name, "--uri", uri] + ([] if self.verify.get_active()
                                                         else ["--no-verify"])
        self.set_sensitive(False)

        def done(ok, output):
            self.destroy()
            if not ok:
                _error(None, "The binhost wasn't added", output or "Cancelled.")
            self.on_done()
        run_as_root(service(*args), done)
