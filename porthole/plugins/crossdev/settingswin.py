"""
    Porthole crossdev plugin: a system's settings window

    Everything the system will be built with, before anything is built:
    profile, compiler, USE flags, hardware, build options, and its Portage
    files. It starts from the profile's values; changes are written in
    Porthole's block of the system's own make.conf (conf.py), as root.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole.plugins.crossdev import conf
from porthole.updater.settings import _facts, _flags, _heading, _small

TABS = (("overview", "Overview"), ("compiler", "Compiler"), ("use", "USE flags"),
        ("hardware", "Hardware"), ("build", "Build options"), ("packages", "Packages"),
        ("firstboot", "First boot"),
        ("files", "Files"))


TITLES_SSHD = "Let me log in over the network (sshd), to finish setting it up from here"


def _host_defaults():
    """This computer's time zone, language, keyboard and user, as a start"""
    import os
    import pwd
    found = {}
    try:
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            found["timezone"] = link.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    lang = os.environ.get("LANG", "")
    if lang and lang not in ("C", "POSIX"):
        found["locale"] = lang.replace(".utf8", ".UTF-8")
    try:
        with open("/etc/conf.d/keymaps") as f:
            for line in f:
                if line.strip().startswith("keymap="):
                    found["keymap"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    try:
        found["user"] = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        pass
    return found


def _cpu_flags_var(arch):
    return {"amd64": "CPU_FLAGS_X86", "x86": "CPU_FLAGS_X86", "arm": "CPU_FLAGS_ARM",
            "arm64": "CPU_FLAGS_ARM", "ppc": "CPU_FLAGS_PPC",
            "ppc64": "CPU_FLAGS_PPC"}.get(arch)


class SystemSettingsWindow(Gtk.Window):
    def __init__(self, name, parent=None, on_changed=None):
        super().__init__(title="Porthole: settings of %s" % name)
        if parent:
            self.set_transient_for(parent)
        self.name, self.on_changed = name, on_changed
        self.set_default_size(820, 640)
        self.info = None
        self.pending = {}  # VAR: value, or None (Porthole doesn't set it)
        self.machine_pending = None  # the computer it is for, changed
        self.search = ""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, border_width=8)
        self.bar = Gtk.InfoBar(message_type=Gtk.MessageType.QUESTION, no_show_all=True)
        self.bar_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=90)
        self.bar.get_content_area().add(self.bar_label)
        self.bar.add_button("Discard", 1)
        self.bar.add_button("Save", 2)
        self.bar.connect("response", self.on_bar)
        box.pack_start(self.bar, False, False, 0)
        self.notebook = Gtk.Notebook()
        self.tabs, self.tab_numbers = {}, {}
        for key, title in TABS:
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=12,
                            valign=Gtk.Align.START)
            inner.pack_start(_small("Reading the settings..."), False, False, 0)
            self.tabs[key] = inner
            if key == "files":
                page = inner
                inner.set_valign(Gtk.Align.FILL)
            else:
                page = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
                page.add(inner)
            self.tab_numbers[key] = self.notebook.append_page(page, Gtk.Label(label=title))
        box.pack_start(self.notebook, True, True, 0)
        buttons = Gtk.Box(spacing=8)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.close())
        buttons.pack_end(close, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self.connect("delete-event", self.on_delete)
        self.show_all()
        self.reload()

    # -- reading --

    def reload(self):
        self.pending = {}
        self.machine_pending = None
        self.update_bar()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        from porthole.plugins.crossdev import roots, toolchain
        from porthole.updater import system
        info = {}
        try:
            target = roots.made_here().get(self.name)
            if target is None:
                raise conf.ConfError("No system called %s was made here" % self.name)
            info = conf.read(target)
            info["target"] = target
            locations = info["locations"]
            info["descriptions"], info["local"] = system.flag_descriptions(locations)
            info["global"] = system.global_flags(locations)
            info["iuse"] = system.all_iuse(locations)
            info["values"] = {n: system.expand_values(n, locations) for n in conf.HARDWARE}
            chost = conf.effective(info, "CHOST")
            info["needs_cross"] = chost != toolchain.host_chost()
            info["toolchain"] = info["toolchain_name"] = None
            info["toolchain_users"] = []
            compiler = None
            if info["needs_cross"]:
                try:
                    resolved = toolchain.resolve(target)
                    found = toolchain.find(resolved)
                    info["toolchain"] = toolchain.compare(resolved, tuple_=found)
                    info["toolchain_name"] = found
                    info["toolchain_users"] = [n for n in toolchain.users(found)
                                               if n != target.name] if found else []
                    compiler = found and found + "-gcc"
                except toolchain.ToolchainError as e:
                    info["toolchain"] = str(e)
            info["cpus"] = conf.cpus(chost, toolchain.host_chost(), compiler) if chost else None
            info["files"] = conf.files(target)
            from porthole.plugins.crossdev import firstboot
            info["firstboot"] = firstboot.read_public(target.name)
            info["firstboot_status"] = firstboot.status(target, info["firstboot"])
            info["zones"] = firstboot.zones()
            info["locales"] = firstboot.locales()
            info["init"] = firstboot.init_system(target)
            info["repos"] = roots.used_repos(target)
            import os
            link = os.path.realpath(os.path.join(target.portage_dir, "make.profile"))
            info["profile_name"] = next(
                ("%s:%s" % (r, link.split(loc.rstrip("/") + "/profiles/", 1)[1])
                 for r, loc in info["repos"].items()
                 if link.startswith(loc.rstrip("/") + "/profiles/")), link)
            from porthole.plugins.crossdev import packages as pk
            info["lookup"] = pk.Lookup(target)
            info["world"] = pk.read_world(target)
            info["package_use"] = pk.read_use(target)
            info["board_meta"] = pk.board_meta(target, info["repos"])
            info["described"] = {a: info["lookup"].describe(a)
                                 for a in info["world"] + info["board_meta"]}
            info["firstboot_packages"] = firstboot.packages(info["firstboot"])
        except Exception as e:
            info = {"error": "%s: %s" % (type(e).__name__, e)}
        GLib.idle_add(self._show, info)

    def _show(self, info):
        if "error" in info:
            self._clear("overview")
            self.tabs["overview"].pack_start(_small("Couldn't read the settings: "
                                                    + info["error"]), False, False, 0)
            self.tabs["overview"].show_all()
            return False
        self.info = info
        self.set_title("Porthole: settings of %s" % info["target"].title)
        for key, _title in TABS:
            self._clear(key)
        self._overview()
        self._compiler()
        self._use()
        self._hardware()
        self._build()
        self._packages_reset()
        self._packages()
        self._firstboot()
        self._files()
        for key in self.tabs:
            self.tabs[key].show_all()
        return False

    def _clear(self, key):
        for child in self.tabs[key].get_children():
            child.destroy()

    def _redraw(self, *keys):
        for key in keys:
            self._clear(key)
            getattr(self, "_" + key)()
            self.tabs[key].show_all()

    # -- changes --

    def values(self):
        """Porthole's block as it would be with the pending changes"""
        values = dict(self.info["porthole"])
        for name, value in self.pending.items():
            if value is None:
                values.pop(name, None)
            else:
                values[name] = value
        return values

    def get(self, name):
        if name == "MAKEOPTS" and self.machine():
            return conf.makeopts(self.machine()["threads"], self.machine()["memory_gb"])
        return conf.effective(self.info, name, self.values())

    def machine(self):
        return self.machine_pending or self.info["machine"]

    def dirty(self):
        return bool(self.pending) or self.machine_pending is not None

    def set(self, name, value):
        """Set Porthole's value of a variable (None: leave it to the profile)"""
        if value is not None:
            value = " ".join(value.split())
            if value == "" and conf.incremental(name):
                value = None
        saved = self.info["porthole"].get(name)
        if value == saved:
            self.pending.pop(name, None)
        else:
            self.pending[name] = value
        self.update_bar()

    def update_bar(self):
        if not self.dirty():
            self.bar.hide()
            return
        parts = []
        if self.machine_pending:
            parts.append("the computer it is for: %d threads, %d GB (MAKEOPTS %s)" % (
                self.machine_pending["threads"], self.machine_pending["memory_gb"],
                self.get("MAKEOPTS")))
        for name, value in sorted(self.pending.items()):
            parts.append("%s back to the profile's" % name if value is None
                         else '%s="%s"' % (name, value))
        self.bar_label.set_markup(
            "<b>Not saved yet:</b> %s\n<small>Saved in Porthole's block of the system's own "
            "make.conf. Nothing is built until you build the system.</small>"
            % GLib.markup_escape_text(", ".join(parts)))
        self.bar.show()
        self.bar_label.show()

    def on_bar(self, bar, response):
        if response == 1:
            self.pending = {}
            self.machine_pending = None
            self.update_bar()
            self._show(self.info)
        elif response == 2:
            self.save()

    def save(self, then=None):
        from porthole.updater.elevate import run_as_root, service
        values = self.values()
        try:
            for name, value in values.items():
                conf.check_value(name, value)
        except conf.ConfError as e:
            self.error("That can't be saved", str(e))
            return
        self.bar.set_sensitive(False)

        def done(ok, output):
            self.bar.set_sensitive(True)
            if not ok:
                self.error("The settings weren't saved", output)
                return
            self.changed()
            if then:
                then()
        request = {"values": values, "machine": self.machine()}
        run_as_root(service("plugin", "crossdev", "settings", self.name, json.dumps(request)),
                    done)

    def changed(self):
        self.reload()
        if self.on_changed:
            self.on_changed()

    def on_delete(self, *args):
        packages = self.info is not None and self._packages_dirty()
        if not self.dirty() and not packages:
            return False
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE,
                                   text="Save the changes to %s?" % self.info["target"].title)
        dialog.add_buttons("Discard", Gtk.ResponseType.NO, "Cancel", Gtk.ResponseType.CANCEL,
                           "Save", Gtk.ResponseType.YES)
        answer = dialog.run()
        dialog.destroy()
        if answer == Gtk.ResponseType.NO:
            return False
        if answer == Gtk.ResponseType.YES:
            if packages and self.dirty():
                self.save(then=lambda: self.save_packages(then=self.destroy))
            elif packages:
                self.save_packages(then=self.destroy)
            else:
                self.save(then=self.destroy)
        return True

    def error(self, text, detail):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text=text)
        dialog.format_secondary_text(detail or "Cancelled.")
        dialog.run()
        dialog.destroy()

    def go_to(self, key):
        self.notebook.set_current_page(self.tab_numbers[key])

    # -- Overview --

    def _overview(self):
        box, info = self.tabs["overview"], self.info
        target = info["target"]
        box.pack_start(_heading(target.title), False, False, 0)
        parts = info["toolchain"]
        if not info["needs_cross"]:
            toolchain = "Not needed: this computer's own compiler builds for it"
        elif isinstance(parts, str):
            toolchain = "Couldn't be worked out: " + parts
        elif not any(built for _t, _w, _r, built in parts):
            toolchain = "Not built yet (Systems window: Build its toolchain)"
        elif not info.get("toolchain_name"):
            # the gcc and binutils its settings want aren't built yet
            toolchain = "Not built for the gcc and binutils its settings want yet " \
                        "(Systems window: Build its toolchain)"
        else:
            toolchain = "%s (gcc %s, binutils %s)%s" % (
                info["toolchain_name"],
                next((w for t, w, _r, _b in parts if t == "gcc"), "?"),
                next((w for t, w, _r, _b in parts if t == "binutils"), "?"),
                ", shared with " + ", ".join(info["toolchain_users"])
                if info.get("toolchain_users") else "")
        box.pack_start(_facts([("Name", target.name), ("Folder", target.root),
                               ("Profile", info["profile_name"]),
                               ("Repositories", ", ".join(info["repos"]) or "?"),
                               ("Cross toolchain", toolchain)]), False, False, 0)
        change = Gtk.Button(label="Change profile…", halign=Gtk.Align.START, margin_start=6)
        change.connect("clicked", lambda b: self.change_profile())
        box.pack_start(change, False, False, 0)

        box.pack_start(_heading("Before building"), False, False, 0)
        box.pack_start(_small(
            "The profile's values are only where the settings start: look through each "
            "tab and set what this system should be."), False, False, 0)
        grid = Gtk.Grid(column_spacing=10, row_spacing=4, margin_start=6)
        for i, (tab, done, text) in enumerate(conf.checklist(info, self.values(),
                                                             self.machine())):
            mark = Gtk.Label(label="✓" if done else "✗", xalign=0)
            grid.attach(mark, 0, i, 1, 1)
            grid.attach(Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=80),
                        1, i, 1, 1)
            go = Gtk.Button(label="Change")
            go.set_relief(Gtk.ReliefStyle.NONE)
            go.connect("clicked", lambda b, t=tab: self.go_to(t))
            grid.attach(go, 2, i, 1, 1)
        box.pack_start(grid, False, False, 0)

    def change_profile(self):
        def switch(choice):
            from porthole.updater.elevate import run_as_root, service

            def done(ok, output):
                if not ok:
                    self.error("The profile wasn't changed", output)
                    return
                self.changed()
            run_as_root(service("plugin", "crossdev", "profile", self.name, choice), done)
        if self.dirty():
            self.error("Save or discard the changes first",
                       "A new profile changes where the settings start.")
            return
        ProfileDialog(self, self.info, switch)

    # -- Compiler --

    def _compiler(self):
        box, info = self.tabs["compiler"], self.info
        chost = self.get("CHOST")
        box.pack_start(_small(
            "Always set for this system's own CPU: never \"native\" (that is the CPU of "
            "the computer doing the build) and not only the kind of CPU (generic), or "
            "packages run slower or not at all on the system."), False, False, 0)
        box.pack_start(_heading("CPU"), False, False, 0)
        row = Gtk.Box(spacing=6, margin_start=6)
        if info["cpus"]:
            combo = Gtk.ComboBoxText.new_with_entry()
            for cpu in info["cpus"]:
                combo.append_text(cpu)
            current = conf.tuning(self.get("CFLAGS"))[0]
            combo.get_child().set_text(current or "")
            row.pack_start(Gtk.Label(label="Build for:"), False, False, 0)
            row.pack_start(combo, False, False, 0)
            use = Gtk.Button(label="Set it in the flags")
            use.set_tooltip_text("Replace -mcpu/-march/-mtune in CFLAGS with %s=<cpu>"
                                 % conf.cpu_option(chost))
            use.connect("clicked", lambda b: self._set_cpu(combo.get_child().get_text()))
            row.pack_start(use, False, False, 0)
            box.pack_start(row, False, False, 0)
            box.pack_start(_small("The CPUs %s's gcc knows." % chost), False, False, 0)
        else:
            box.pack_start(_small(
                "The list of CPUs comes from the system's own compiler: build its toolchain "
                "first, or write the CPU options in the flags below."), False, False, 0)
        self.compiler_entries = {}
        grid = Gtk.Grid(column_spacing=8, row_spacing=2, margin_start=6, margin_top=6)
        row = 0
        for name in conf.COMPILER:
            if name == "MAKEOPTS":
                continue
            label = Gtk.Label(label=name, xalign=0)
            label.set_tooltip_text(conf.NOTES.get(name, ""))
            grid.attach(label, 0, row, 1, 1)
            entry = Gtk.Entry(hexpand=True, text=self.get(name))
            entry.connect("changed", lambda e, n=name: self._entry_changed(n, e))
            grid.attach(entry, 1, row, 1, 1)
            back = Gtk.Button(label="Profile's")
            back.set_tooltip_text("Back to what the profile says")
            back.connect("clicked", lambda b, n=name, e=entry: e.set_text(
                self.info["profile"].get(n, "")))
            grid.attach(back, 2, row, 1, 1)
            self.compiler_entries[name] = entry
            note = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
            grid.attach(note, 1, row + 1, 2, 1)
            entry.note = note
            self._entry_note(name, entry)
            row += 2
        box.pack_start(grid, False, False, 0)
        box.pack_start(_small(
            "${CFLAGS} in CXXFLAGS and the others means the same as CFLAGS."), False, False, 0)
        box.pack_start(_heading("Parallel builds (MAKEOPTS)"), False, False, 0)
        self.makeopts_label = _small("")
        box.pack_start(self.makeopts_label, False, False, 0)
        self._makeopts_note()

    def _makeopts_note(self):
        machine = self.machine()
        if machine:
            text = ("%s when emerge is run by hand on it: worked out from the computer it is "
                    "for (Hardware: %d CPU threads, %d GB). Porthole's own builds work it out "
                    "again for each build, from the machines that help compile."
                    % (self.get("MAKEOPTS"), machine["threads"], machine["memory_gb"]))
        else:
            text = ("Set by Porthole once it knows the computer it is for: its CPU threads "
                    "and memory (Hardware tab).")
            if self.get("MAKEOPTS"):
                text += " Now: " + self.get("MAKEOPTS")
        self.makeopts_label.set_markup("<small>%s</small>" % GLib.markup_escape_text(text))

    def _machine_changed(self, threads, memory):
        machine = {"threads": int(threads.get_value()), "memory_gb": int(memory.get_value())}
        self.machine_pending = None if machine == self.info["machine"] else machine
        self.makeopts.set_markup("<small>MAKEOPTS: %s</small>" % GLib.markup_escape_text(
            conf.makeopts(machine["threads"], machine["memory_gb"])))
        self.update_bar()
        self._makeopts_note()
        self._redraw("overview")

    def _set_cpu(self, cpu):
        cpu = cpu.strip()
        if not cpu:
            return
        entry = self.compiler_entries["CFLAGS"]
        entry.set_text(conf.set_cpu(entry.get_text(), self.get("CHOST"), cpu))

    def _entry_changed(self, name, entry):
        text = entry.get_text()
        profile = self.info["profile"].get(name)
        own = self.info["own"].get(name)
        if own is None and " ".join(text.split()) == " ".join((profile or "").split()) \
                and name not in self.info["porthole"] and name not in ("CHOST", "CFLAGS"):
            self.set(name, None)
        else:
            self.set(name, text)
        self._entry_note(name, entry)
        self._redraw("overview")

    def _entry_note(self, name, entry):
        parts = []
        profile = self.info["profile"].get(name)
        parts.append("Profile: %s" % (profile if profile else "(not set)"))
        if name in self.info["own"]:
            parts.append("Your own line in make.conf sets %s; it wins over this "
                         "(Files tab)" % name)
        if name in ("CFLAGS", "CXXFLAGS"):
            text = entry.get_text()
            if name == "CXXFLAGS" and text.strip() in ("${CFLAGS}", "$CFLAGS"):
                pass
            else:
                problem = conf.tuning(text)[1]
                if problem:
                    parts.append("⚠ " + problem)
        entry.note.set_markup("<small>%s</small>" % GLib.markup_escape_text(
            ". ".join(parts)))

    # -- words of incremental variables (USE, VIDEO_CARDS, ...) --

    def word_on(self, name, word):
        return word in self.get(name).split()

    def toggle(self, name, word):
        """Switch one word of an incremental variable in Porthole's block;
        back to what the profile says takes Porthole's word away"""
        want = not self.word_on(name, word)
        words = [w for w in (self.values().get(name) or "").split()
                 if w.lstrip("-") != word]
        without = dict(self.values())
        without[name] = " ".join(words)
        if (word in conf.effective(self.info, name, without).split()) != want:
            words.append(word if want else "-" + word)
        self.set(name, " ".join(words))
        # after the click's own handler: it belongs to a widget the redraw replaces
        GLib.idle_add(lambda: self._redraw(*{"USE": ("use", "overview")}.get(
            name, ("hardware", "overview"))) and False)

    def changed_words(self, name):
        """(words Porthole's block sets, words not saved yet)"""
        now = set((self.values().get(name) or "").split())
        saved = set((self.info["porthole"].get(name) or "").split())
        return {w.lstrip("-") for w in now}, {w.lstrip("-") for w in now ^ saved}

    def source(self, name, word):
        if word in {w.lstrip("-") for w in self.info["own"].get(name, "").split()}:
            return "Set by your own line in make.conf (it wins; Files tab)"
        if word in {w.lstrip("-") for w in (self.values().get(name) or "").split()}:
            return "Set here" + ("" if name not in self.pending else ", not saved yet")
        return "From the profile"

    # -- USE flags --

    def _use(self):
        from porthole.updater.system import categorise, category_order
        box, info = self.tabs["use"], self.info
        prefixes = tuple(n.lower() + "_" for n in info["expand"] + info["hidden"])
        arch = info["arch"]

        def plain(flag):
            return not flag.startswith(prefixes) and flag != arch and flag

        box.pack_start(_small(
            "Click a flag to switch it for every package on this system; hover for what it "
            "does. Dimmed: off. •: set here rather than by the profile."), False, False, 0)
        find = Gtk.SearchEntry(placeholder_text="Find a flag to add (name or what it does)",
                               text=self.search)
        find.connect("search-changed", self._search_changed)
        box.pack_start(find, False, False, 0)
        # the flags shown: refilled as you type (the search box itself stays)
        self.use_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(self.use_results, False, False, 0)
        self._use_results()

    def _use_results(self):
        from porthole.updater.system import categorise, category_order
        box, info = self.use_results, self.info
        prefixes = tuple(n.lower() + "_" for n in info["expand"] + info["hidden"])
        arch = info["arch"]

        def plain(flag):
            return not flag.startswith(prefixes) and flag != arch and flag
        mine, pending = self.changed_words("USE")
        on = {w for w in self.get("USE").split() if plain(w)}
        if self.search:
            needle = self.search.lower()
            shown = {f for f, d in info["descriptions"].items()
                     if plain(f) and (needle in f.lower() or needle in d.lower())}
            if len(shown) > 400:
                box.pack_start(_small("%d flags match; showing the first 400." % len(shown)),
                               False, False, 0)
                shown = set(sorted(shown)[:400])
        else:
            shown = on | mine
            box.pack_start(_small(
                "Showing the flags that are on (%d) and the ones set here. Find others above."
                % len(on)), False, False, 0)
        groups = {}
        for flag in shown:
            groups.setdefault(categorise(flag, info["descriptions"], info["local"]),
                              []).append(flag)
        for title in category_order():
            if groups.get(title):
                box.pack_start(_heading(title), False, False, 0)
                box.pack_start(_flags(
                    sorted(groups[title]), info["descriptions"], info["iuse"],
                    on_click=lambda f: self.toggle("USE", f),
                    state=lambda f: self.word_on("USE", f), changed=mine, pending=pending,
                    source=lambda f: self.source("USE", f),
                    packages={f: p for f, p in info["local"].items()
                              if f not in info["global"]}), False, False, 0)

    def _search_changed(self, entry):
        # never destroy the entry from its own signal: only the results
        self.search = entry.get_text().strip()
        GLib.idle_add(self._refill_use_results)

    def _refill_use_results(self):
        for child in self.use_results.get_children():
            child.destroy()
        self._use_results()
        self.use_results.show_all()
        return False

    # -- Hardware --

    def _hardware(self):
        box, info = self.tabs["hardware"], self.info
        box.pack_start(_heading("The computer it is for"), False, False, 0)
        machine = self.machine() or {}
        row = Gtk.Box(spacing=6, margin_start=6)
        threads = Gtk.SpinButton.new_with_range(1, 1024, 1)
        threads.set_value(machine.get("threads", 4))
        memory = Gtk.SpinButton.new_with_range(1, 65536, 1)
        memory.set_value(machine.get("memory_gb", 4))
        row.pack_start(threads, False, False, 0)
        row.pack_start(Gtk.Label(label="CPU threads"), False, False, 0)
        row.pack_start(memory, False, False, 0)
        row.pack_start(Gtk.Label(label="GB of memory"), False, False, 0)
        self.makeopts = Gtk.Label(xalign=0, margin_start=12)
        row.pack_start(self.makeopts, False, False, 0)
        box.pack_start(row, False, False, 0)
        if machine:
            self.makeopts.set_markup("<small>MAKEOPTS: %s</small>" % GLib.markup_escape_text(
                self.get("MAKEOPTS")))
        else:
            self.makeopts.set_markup("<small>Not set yet: set them, Porthole works out "
                                     "MAKEOPTS (one job per 2 GB, at most one per thread)"
                                     "</small>")
            confirm = Gtk.Button(label="These are right")
            confirm.connect("clicked", lambda b: self._machine_changed(threads, memory))
            row.pack_start(confirm, False, False, 0)
        for spin in (threads, memory):
            spin.connect("value-changed", lambda s: self._machine_changed(threads, memory))
        box.pack_start(_small(
            "What the computer this system is for has: packages build support for exactly "
            "these. Nothing is found out here (this isn't that computer): choose them. "
            "Click to switch one; hover for what it is."), False, False, 0)
        names = ["VIDEO_CARDS", "INPUT_DEVICES"]
        cpu_flags = _cpu_flags_var(info["arch"])
        if cpu_flags:
            names.append(cpu_flags)
        names += ["L10N"]
        for name in names:
            values = dict(info["values"].get(name) or {})
            on = self.get(name).split()
            for word in on:
                values.setdefault(word, "")
            mine, pending = self.changed_words(name)
            flow = _flags(sorted(values), values, None,
                          on_click=lambda w, n=name: self.toggle(n, w),
                          state=lambda w, n=name: self.word_on(n, w), changed=mine,
                          pending=pending, source=lambda w, n=name: self.source(n, w))
            title = "%s: %s" % (name, " ".join(on) or "none")
            if name == "L10N":
                expander = Gtk.Expander(label="%s (languages, %d)" % (title, len(values)))
                expander.add(flow)
                box.pack_start(expander, False, False, 0)
            else:
                box.pack_start(_heading(title), False, False, 0)
                box.pack_start(flow, False, False, 0)
        if cpu_flags:
            box.pack_start(_small(
                "%s: what the CPU can do beyond the basics. On the computer itself "
                "cpuid2cpuflags (app-portage/cpuid2cpuflags) lists them." % cpu_flags),
                False, False, 0)

    # -- Build options --

    def _build(self):
        box, info = self.tabs["build"], self.info
        arch = info["arch"]
        box.pack_start(_heading("Package versions"), False, False, 0)
        testing = "~" + arch in self.get("ACCEPT_KEYWORDS").split()
        stable = Gtk.RadioButton.new_with_label(None, "Stable only (%s)" % arch)
        test = Gtk.RadioButton.new_with_label_from_widget(stable, "Testing too (~%s)" % arch)
        (test if testing else stable).set_active(True)
        test.connect("toggled", lambda b: self._testing(b.get_active()))
        for widget in (stable, test):
            widget.set_margin_start(6)
            box.pack_start(widget, False, False, 0)
        if "ACCEPT_KEYWORDS" in info["own"]:
            box.pack_start(_small("Your own line in make.conf sets ACCEPT_KEYWORDS too."),
                           False, False, 0)
        for name, text in (
                ("FEATURES", "How portage builds (distcc, -pid-sandbox, ...): changes to the "
                             "profile's, like \"distcc -pid-sandbox\"."),
                ("ACCEPT_LICENSE", "Which licences are fine: changes to the profile's "
                                   "(\"%s\"), like \"@BINARY-REDISTRIBUTABLE\"."
                                   % info["profile"].get("ACCEPT_LICENSE", ""))):
            box.pack_start(_heading(name), False, False, 0)
            box.pack_start(_small(text), False, False, 0)
            entry = Gtk.Entry(text=self.values().get(name) or "", margin_start=6)
            entry.connect("changed", lambda e, n=name: (self.set(n, e.get_text()),
                                                        self._redraw("overview")))
            box.pack_start(entry, False, False, 0)
            box.pack_start(_small("Now: " + (self.get(name) or "(none)")), False, False, 0)
        box.pack_start(_small(
            "Binary packages: \"Keeps packages\" in the Systems window.", margin_top=6),
            False, False, 0)

    def _testing(self, on):
        words = [w for w in (self.values().get("ACCEPT_KEYWORDS") or "").split()
                 if w.lstrip("-") != "~" + self.info["arch"]]
        if on:
            words.append("~" + self.info["arch"])
        self.set("ACCEPT_KEYWORDS", " ".join(words))
        self._redraw("overview")

    # -- Packages --

    def _packages_reset(self):
        self.world = list(self.info["world"])
        self.package_use = {k: list(v) for k, v in self.info["package_use"].items()}
        self.selected_package = None

    def _packages_dirty(self):
        return (self.world != self.info["world"]
                or self.package_use != self.info["package_use"])

    def _describe(self, atom):
        if atom not in self.info["described"]:
            self.info["described"][atom] = self.info["lookup"].describe(atom)
        return self.info["described"][atom]

    def _packages(self):
        from porthole.plugins.crossdev import packages as pk
        box, info = self.tabs["packages"], self.info
        box.pack_start(_small(
            "What the system installs on top of its base (@system): its world file, as on any "
            "Gentoo system. Porthole on the system finds the same list there."), False, False, 0)
        if self._packages_dirty():
            bar = Gtk.Box(spacing=8)
            bar.pack_start(_small("Not saved yet."), False, False, 0)
            save = Gtk.Button(label="Save the package list")
            save.connect("clicked", lambda b: self.save_packages())
            undo = Gtk.Button(label="Discard")
            undo.connect("clicked", lambda b: (self._packages_reset(),
                                               self._redraw("packages")))
            bar.pack_end(save, False, False, 0)
            bar.pack_end(undo, False, False, 0)
            box.pack_start(bar, False, False, 0)
        # the board's base
        for meta in info["board_meta"]:
            if meta not in self.world:
                described = self._describe(meta)
                row = Gtk.Box(spacing=8, margin_start=6)
                row.pack_start(_small("Board base from the profile's repository: %s (%s)"
                                      % (meta, described["description"])), False, False, 0)
                add = Gtk.Button(label="Add")
                add.connect("clicked", lambda b, m=meta: self._add_package(m))
                row.pack_end(add, False, False, 0)
                box.pack_start(row, False, False, 0)
        # starter lists
        row = Gtk.Box(spacing=6)
        starter = Gtk.ComboBoxText()
        for i, (title, _text, _list, _use) in enumerate(pk.STARTERS):
            starter.append(str(i), title)
        starter.set_active(0)
        row.pack_start(Gtk.Label(label="Start from:"), False, False, 0)
        row.pack_start(starter, True, True, 0)
        add_list = Gtk.Button(label="Add these")
        add_list.connect("clicked", lambda b: self._add_starter(
            pk.STARTERS[int(starter.get_active_id())]))
        row.pack_start(add_list, False, False, 0)
        box.pack_start(row, False, False, 0)
        def note(entry):
            text = entry[1]
            if entry[3]:
                text += " Also sets USE for every package: %s." % " ".join(entry[3])
            return text
        starter_note = _small(note(pk.STARTERS[0]))
        starter.connect("changed", lambda c: starter_note.set_markup(
            "<small>%s</small>" % GLib.markup_escape_text(note(pk.STARTERS[int(c.get_active_id())]))))
        box.pack_start(starter_note, False, False, 0)
        # find one
        find = Gtk.SearchEntry(placeholder_text="Find a package to add")
        box.pack_start(find, False, False, 0)
        results = Gtk.ListStore(str, str)
        results_view = Gtk.TreeView(model=results, headers_visible=False, no_show_all=True)
        results_view.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=0))
        cell = Gtk.CellRendererText(ellipsize=3)
        results_view.append_column(Gtk.TreeViewColumn("", cell, text=1))
        results_scroll = Gtk.ScrolledWindow(min_content_height=140, no_show_all=True)
        results_scroll.add(results_view)
        box.pack_start(results_scroll, False, False, 0)

        def search(entry):
            results.clear()
            found = info["lookup"].find(entry.get_text())
            for cp in found[:40]:
                results.append([cp, self._describe(cp)["description"]])
            results_scroll.set_visible(bool(found))
            results_view.set_visible(bool(found))
        find.connect("search-changed", search)
        results_view.connect("row-activated", lambda v, path, c: self._add_package(
            results[path][0]))
        box.pack_start(_small("Double-click a package found to add it."), False, False, 0)

        # the list
        box.pack_start(_heading("Installs (%d)" % len(self.world)), False, False, 0)
        store = Gtk.ListStore(str, str, str, str)
        for atom in self.world:
            d = self._describe(atom)
            store.append([atom, d["cpv"].split("/", 1)[1] if d["cpv"] else "",
                          d["repo"] or "", d["problem"] or d["description"]])
        for atom in info["firstboot_packages"]:
            if atom not in self.world:
                d = self._describe(atom)
                store.append([atom, d["cpv"].split("/", 1)[1] if d["cpv"] else "",
                              d["repo"] or "", "for the First boot tab's choices"])
        view = Gtk.TreeView(model=store)
        for i, title in enumerate(("Package", "Would install", "From", "About")):
            renderer = Gtk.CellRendererText()
            if i == 3:
                renderer.set_property("ellipsize", 3)
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            column.set_resizable(True)
            column.set_expand(i == 3)
            view.append_column(column)
        view.set_tooltip_column(3)
        scroll = Gtk.ScrolledWindow(min_content_height=200)
        scroll.add(view)
        box.pack_start(scroll, False, False, 0)
        missing = [a for a in self.world if self._describe(a)["problem"]]
        for atom in missing:
            d = self._describe(atom)
            hint = ""
            if d["problem"].startswith("Not in the repositories"):
                hint = (" Tick the repository that has it for %s (Repositories, "
                        "“Repositories of: %s”)." % (info["target"].title,
                                                              info["target"].title))
            elif "keyworded" in d["problem"]:
                hint = " Allow testing versions of it (Files: package.accept_keywords)."
            box.pack_start(_small("⚠ %s: %s.%s" % (atom, d["problem"], hint)),
                           False, False, 0)
        buttons = Gtk.Box(spacing=6)
        remove = Gtk.Button(label="Remove")
        remove.connect("clicked", lambda b: self._remove_package(view))
        buttons.pack_start(remove, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.package_flags = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(self.package_flags, False, False, 0)
        view.get_selection().connect("changed", lambda sel: self._package_selected(sel))
        if self.selected_package:
            for row in store:
                if row[0] == self.selected_package:
                    view.get_selection().select_iter(row.iter)

    def set_word(self, name, word, on):
        """Turn one word of an incremental variable on or off (pending)"""
        if self.word_on(name, word) == on:
            return
        words = [w for w in (self.values().get(name) or "").split() if w.lstrip("-") != word]
        without = dict(self.values())
        without[name] = " ".join(words)
        if (word in conf.effective(self.info, name, without).split()) != on:
            words.append(word if on else "-" + word)
        self.set(name, " ".join(words))

    def _add_starter(self, starter):
        _title, _text, atoms, use = starter
        for atom in atoms:
            self._add_package(atom, redraw=False)
        # the USE flags the list needs, as changes not saved yet (USE tab)
        for word in use:
            self.set_word("USE", word.lstrip("-"), not word.startswith("-"))
        self._redraw("packages", "use", "overview")

    def _add_package(self, atom, redraw=True):
        if atom not in self.world:
            self.world.append(atom)
        if redraw:
            self.selected_package = atom
            self._redraw("packages")

    def _remove_package(self, view):
        model, row = view.get_selection().get_selected()
        if row and model[row][0] in self.world:
            self.world.remove(model[row][0])
            self.package_use.pop(model[row][0], None)
            self.selected_package = None
            self._redraw("packages")

    def _package_selected(self, selection):
        from porthole.plugins.crossdev import packages as pk
        for child in self.package_flags.get_children():
            child.destroy()
        model, row = selection.get_selected()
        if not row:
            return
        atom = model[row][0]
        self.selected_package = atom
        d = self._describe(atom)
        if not d["cpv"] or not d["iuse"]:
            return
        state = self.info["lookup"].use_state(d["cpv"])
        mine = {f.lstrip("-"): not f.startswith("-") for f in self.package_use.get(atom, [])}
        location = self.info["repos"].get(d["repo"]) or ""
        described = pk.flag_descriptions(location, d["cp"])
        described = {f: described.get(f) or self.info["descriptions"].get(f, "")
                     for f in state}

        def on(flag):
            return mine.get(flag, state[flag])

        def toggle(flag):
            words = [w for w in self.package_use.get(atom, []) if w.lstrip("-") != flag]
            if (not on(flag)) != state[flag]:
                words.append(flag if not on(flag) else "-" + flag)
            if words:
                self.package_use[atom] = words
            else:
                self.package_use.pop(atom, None)
            GLib.idle_add(lambda: self._redraw("packages") and False)
        self.package_flags.pack_start(_heading("USE flags of %s" % atom), False, False, 0)
        self.package_flags.pack_start(_flags(
            sorted(state), described, None, on_click=toggle, state=on, changed=set(mine),
            pending=set(), source=lambda f: "Set here for %s" % atom if f in mine else
            "As the system would build it"), False, False, 0)
        self.package_flags.show_all()

    def save_packages(self, then=None):
        from porthole.updater.elevate import run_as_root, service
        request = {"world": self.world, "use": self.package_use}

        def done(ok, output):
            if not ok:
                self.error("The package list wasn't saved", output)
                return
            self.changed()
            if then:
                then()
        run_as_root(service("plugin", "crossdev", "packages", self.name, json.dumps(request)),
                    done)

    # -- First boot --

    def _firstboot(self):
        from porthole.plugins.crossdev import firstboot as fb
        box, info = self.tabs["firstboot"], self.info
        saved = info["firstboot"]
        box.pack_start(_small(
            "Just enough that the system starts, lets you log in and gets online; Porthole on "
            "the system takes over from there. Written into the system at the end of its "
            "build, after its packages. Empty: left to the system's packages (a board's "
            "repository may bring its own)."), False, False, 0)
        if not saved:
            box.pack_start(_small("Nothing saved yet: filled in from this computer as a start."),
                           False, False, 0)
        host = _host_defaults()
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin_start=6)
        self.fb = {}

        def combo(values, current):
            widget = Gtk.ComboBoxText.new_with_entry()
            completion = Gtk.EntryCompletion(model=Gtk.ListStore(str), text_column=0)
            for value in values:
                widget.append_text(value)
                completion.get_model().append([value])
            completion.set_match_func(lambda c, key, it: key.lower() in
                                      (c.get_model()[it][0] or "").lower())
            widget.get_child().set_completion(completion)
            widget.get_child().set_text(current or "")
            return widget

        rows = [("hostname", "Computer name", Gtk.Entry(text=saved.get(
                    "hostname", self.info["target"].name))),
                ("timezone", "Time zone", combo(info["zones"], saved.get(
                    "timezone", host.get("timezone")))),
                ("locale", "Language", combo(info["locales"], saved.get(
                    "locale", host.get("locale")))),
                ("keymap", "Keyboard (text console)", combo(fb.KEYMAPS, saved.get(
                    "keymap", host.get("keymap"))))]
        for i, (key, title, widget) in enumerate(rows):
            grid.attach(Gtk.Label(label=title, xalign=0), 0, i, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, i, 2, 1)
            self.fb[key] = widget
        row = len(rows)

        def password_pair(row, title, is_set):
            grid.attach(Gtk.Label(label=title, xalign=0), 0, row, 1, 1)
            first = Gtk.Entry(visibility=False, hexpand=True,
                              placeholder_text="set already: leave empty to keep it"
                              if is_set else "password")
            second = Gtk.Entry(visibility=False, hexpand=True, placeholder_text="again")
            grid.attach(first, 1, row, 1, 1)
            grid.attach(second, 2, row, 1, 1)
            return first, second
        self.fb["root"] = password_pair(row, "root password", saved.get("root_set"))
        row += 1
        user = saved.get("user") or {}
        grid.attach(Gtk.Label(label="Your account", xalign=0), 0, row, 1, 1)
        self.fb["user"] = Gtk.Entry(text=user.get("name", host.get("user", "")),
                                    placeholder_text="user name (empty: none)")
        grid.attach(self.fb["user"], 1, row, 1, 1)
        self.fb["admin"] = Gtk.CheckButton(label="may run things as root (sudo)")
        self.fb["admin"].set_active(user.get("admin", True))
        grid.attach(self.fb["admin"], 2, row, 1, 1)
        row += 1
        self.fb["user_password"] = password_pair(row, "Its password",
                                                 user.get("password_set"))
        box.pack_start(grid, False, False, 0)

        box.pack_start(_heading("Network at boot"), False, False, 0)
        first = None
        self.fb["network"] = {}
        for key, (_pkg, _service, title) in fb.NETWORK.items():
            button = (Gtk.RadioButton.new_with_label(None, title) if first is None else
                      Gtk.RadioButton.new_with_label_from_widget(first, title))
            first = first or button
            button.set_margin_start(6)
            button.set_active(saved.get("network", "networkmanager") == key)
            self.fb["network"][key] = button
            box.pack_start(button, False, False, 0)
        box.pack_start(_heading("Wi-Fi through"), False, False, 0)
        first = None
        self.fb["wifi"] = {}
        for key, title in fb.WIFI.items():
            button = (Gtk.RadioButton.new_with_label(None, title) if first is None else
                      Gtk.RadioButton.new_with_label_from_widget(first, title))
            first = first or button
            button.set_margin_start(6)
            button.set_active(saved.get("wifi", "wpa_supplicant") == key)
            self.fb["wifi"][key] = button
            box.pack_start(button, False, False, 0)
        box.pack_start(_small(
            "The other one is masked in the system, so nothing installs it, and NetworkManager "
            "is built for the one chosen."), False, False, 0)
        self.fb["sshd"] = Gtk.CheckButton(label=TITLES_SSHD, margin_start=6)
        self.fb["sshd"].set_active(saved.get("sshd", False))
        box.pack_start(self.fb["sshd"], False, False, 0)
        box.pack_start(_small(
            "Their packages (and sudo for an account that may run things as root) are added "
            "to what the system installs."), False, False, 0)

        save = Gtk.Button(label="Save the first-boot setup", halign=Gtk.Align.START)
        save.connect("clicked", lambda b: self.save_firstboot())
        box.pack_start(save, False, False, 0)
        self.fb_message = Gtk.Label(xalign=0, wrap=True, max_width_chars=90)
        box.pack_start(self.fb_message, False, False, 0)

        box.pack_start(_heading("Where each comes from (%s)" % info["init"]), False, False, 0)
        box.pack_start(_facts([(title, source) for _item, title, source
                               in info["firstboot_status"]]), False, False, 0)

    def _fb_text(self, key):
        widget = self.fb[key]
        if isinstance(widget, Gtk.ComboBoxText):
            widget = widget.get_child()
        return widget.get_text().strip()

    def save_firstboot(self):
        import os
        import tempfile
        from porthole.plugins.crossdev import firstboot as fb
        from porthole.updater.elevate import run_as_root, service

        def problem(text):
            self.fb_message.set_markup("<small>\u26a0 %s</small>" % GLib.markup_escape_text(text))
        config = {key: self._fb_text(key) for key in ("hostname", "timezone", "locale",
                                                      "keymap")}
        config = {k: v for k, v in config.items() if v}
        if config.get("timezone") and config["timezone"] not in self.info["zones"]:
            return problem("Not a time zone: %s" % config["timezone"])
        if config.get("locale") and config["locale"] not in self.info["locales"]:
            return problem("Not a language this computer knows: %s" % config["locale"])
        try:
            for key, target in (("root", "root_hash"), ("user_password", "user_hash")):
                first, second = (e.get_text() for e in self.fb[key])
                if first != second:
                    return problem("The two %s entries differ" % (
                        "root password" if key == "root" else "account password"))
                if first:
                    config[target] = fb.hash_password(first)
            name = self._fb_text("user")
            if name:
                config["user"] = {"name": name, "hash": config.pop("user_hash", ""),
                                  "admin": self.fb["admin"].get_active()}
            else:
                config.pop("user_hash", None)
            config["network"] = next(k for k, b in self.fb["network"].items()
                                     if b.get_active())
            config["wifi"] = next(k for k, b in self.fb["wifi"].items() if b.get_active())
            config["sshd"] = self.fb["sshd"].get_active()
            fb.check(config)
        except fb.FirstBootError as e:
            return problem(str(e))
        cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or
                             os.path.expanduser("~/.cache"), "porthole")
        os.makedirs(cache, mode=0o700, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="firstboot-", suffix=".json", dir=cache)
        with os.fdopen(fd, "w") as f:  # mkstemp: mode 0600
            json.dump(config, f)

        def done(ok, output):
            try:
                os.unlink(path)
            except OSError:
                pass
            if not ok:
                self.error("The first-boot setup wasn't saved", output)
                return
            self.changed()
        run_as_root(service("plugin", "crossdev", "firstboot", self.name, path), done)

    # -- Files --

    def _files(self):
        box = self.tabs["files"]
        box.pack_start(_small(
            "The system's own Portage files, for anything the other tabs don't cover. "
            "Porthole's block in make.conf is what the tabs write; leave it to them."),
            False, False, 0)
        row = Gtk.Box(spacing=6)
        self.file_combo = Gtk.ComboBoxText()
        for path in self.info["files"]:
            self.file_combo.append(path, path)
        row.pack_start(self.file_combo, False, False, 0)
        new = Gtk.Button(label="New file…")
        new.connect("clicked", lambda b: self.new_file())
        row.pack_start(new, False, False, 0)
        self.file_save = Gtk.Button(label="Save this file")
        self.file_save.connect("clicked", lambda b: self.save_file())
        row.pack_end(self.file_save, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.file_view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.file_view)
        box.pack_start(scroller, True, True, 0)
        self.file_combo.connect("changed", lambda c: self.open_file(c.get_active_id()))
        self.file_combo.set_active_id("make.conf")

    def open_file(self, relative):
        import os
        if not relative:
            return
        try:
            with open(conf.file_path(self.info["target"], relative)) as f:
                text = f.read()
        except (OSError, conf.ConfError):
            text = ""
        self.file_view.get_buffer().set_text(text)
        self.file_view.get_buffer().set_modified(False)
        self.file_relative = relative

    def save_file(self):
        from porthole.updater.elevate import run_as_root, service
        if self.dirty():
            self.error("Save or discard the changes above first",
                       "They are written to make.conf too.")
            return
        buf = self.file_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        relative = getattr(self, "file_relative", None)
        if not relative:
            return

        def done(ok, output):
            if not ok:
                self.error("%s wasn't saved" % relative, output)
                return
            self.changed()
        run_as_root(service("plugin", "crossdev", "write-file", self.name, relative,
                            json.dumps(text)), done)

    def new_file(self):
        dialog = Gtk.Dialog(title="New settings file", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_border_width(12)
        area.set_spacing(6)
        folder = Gtk.ComboBoxText()
        for name in conf.FILE_DIRS:
            folder.append(name, name)
        folder.set_active(0)
        entry = Gtk.Entry(placeholder_text="file name, like porthole or desktop")
        area.pack_start(Gtk.Label(label="In:", xalign=0), False, False, 0)
        area.pack_start(folder, False, False, 0)
        area.pack_start(Gtk.Label(label="Name:", xalign=0), False, False, 0)
        area.pack_start(entry, False, False, 0)
        dialog.show_all()
        answer = dialog.run()
        relative = "%s/%s" % (folder.get_active_id(), entry.get_text().strip())
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return
        try:
            conf.file_path(self.info["target"], relative)
        except conf.ConfError as e:
            self.error("That file can't be made", str(e))
            return
        if relative not in self.info["files"]:
            self.info["files"].append(relative)
            self.file_combo.append(relative, relative)
        self.file_combo.set_active_id(relative)
        self.file_view.get_buffer().set_text("")


class ProfileDialog(Gtk.Dialog):
    """Another profile from the repositories the system uses"""

    def __init__(self, parent, info, on_chosen):
        super().__init__(title="Porthole: profile of %s" % info["target"].title,
                         transient_for=parent, modal=True)
        from porthole.plugins.crossdev import roots
        self.set_default_size(600, 480)
        self.info, self.on_chosen = info, on_chosen
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        area.pack_start(_small(
            "The profile is where this system's settings start (Porthole's own settings "
            "stay as they are), and which packages make up its base. Now: %s"
            % info["profile_name"]), False, False, 0)
        self.all = roots.profiles(info["repos"])
        self.store = Gtk.ListStore(str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, title in enumerate(("Profile", "Architecture", "Status")):
            self.view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        area.pack_start(scroller, True, True, 0)
        self.everything = Gtk.CheckButton(label="Also other architectures and dev/exp profiles")
        self.everything.connect("toggled", lambda b: self.fill())
        area.pack_start(self.everything, False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Switch", Gtk.ResponseType.OK)
        self.fill()
        self.show_all()
        answer = self.run()
        model, row = self.view.get_selection().get_selected()
        choice = model[row][0] if row else None
        self.destroy()
        if answer == Gtk.ResponseType.OK and choice and choice != info["profile_name"]:
            on_chosen(choice)

    def fill(self):
        self.store.clear()
        everything = self.everything.get_active()
        for repo, arch, profile, status in self.all:
            name = "%s:%s" % (repo, profile)
            if everything or (arch == self.info["arch"] and status == "stable") \
                    or name == self.info["profile_name"]:
                row = self.store.append([name, arch, status])
                if name == self.info["profile_name"]:
                    self.view.get_selection().select_iter(row)
