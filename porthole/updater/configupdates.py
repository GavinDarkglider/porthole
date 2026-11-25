"""
    Porthole: configuration file updates

    A package doesn't overwrite a configuration file you changed (one under
    CONFIG_PROTECT): it puts its new version next to it as ._cfg0000_<name>
    and leaves the choice to you (etc-update, dispatch-conf). Porthole finds
    them, sorts out the ones that need no decision, and shows the rest:

      same        the new version is your file: nothing to do
      comments    they differ only in comments and blank lines: the new one
      untouched   you never changed the version Porthole last saw (its
                  copy, below): the new one
      upstream    the package didn't change it since then: yours
      merged      both changed, in different places: merged (three-way, diff3)
      conflict    both changed the same lines: yours to decide
      settings    a KEY=value file: the new file with your values kept,
                  shown for you to save
      differs     anything else: the differences, for you to decide
      unreadable  only root can read it: keep or take the new one

    Each version you settle on is kept in /var/lib/porthole/config-archive
    (readable like the file itself), so the next update of that file can be
    merged three-way. Listing works as any user; changing files goes
    through the service as root (config-updates apply/auto), and only for
    files that really have an update waiting.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import difflib
import os
import re
import subprocess
import tempfile

ARCHIVE_DIR = "/var/lib/porthole/config-archive"
_CFG = re.compile(r"^\._cfg(\d{4})_(.+)$")
_SETTING = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=(.*)$")
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# the kinds settled without asking, and how
AUTOMATIC = {"same": "keep", "comments": "new", "untouched": "new", "upstream": "keep",
             "merged": "merged"}
ACTIONS = ("keep", "new", "merged")


class ConfigUpdateError(Exception):
    pass


class Update:
    """One configuration file with new versions waiting"""

    def __init__(self, root, live, updates):
        self.live = live            # the file on disk
        self.updates = sorted(updates)
        self.newest = self.updates[-1]
        rel = os.path.relpath(live, root)
        self.name = "/" + rel       # its path in the system

    def __repr__(self):
        return "Update(%r, %d waiting)" % (self.name, len(self.updates))


def protect_lists(target):
    """(CONFIG_PROTECT, CONFIG_PROTECT_MASK) as portage reads them for target"""
    import portage
    if target.local:
        settings = portage.settings
    else:
        settings = portage.config(config_root=target.config_root, target_root=target.root)
    return settings.get("CONFIG_PROTECT", "").split(), settings.get("CONFIG_PROTECT_MASK",
                                                                     "").split()


def _under(name, paths):
    return any(name == p.rstrip("/") or name.startswith(p.rstrip("/") + "/") for p in paths)


def pending(target, protect=None, mask=None):
    """[Update] in target, as portage would find them (protected folders,
    hidden folders skipped), listed without needing root"""
    if protect is None:
        protect, mask = protect_lists(target)
    root = os.path.realpath(target.root)
    found = {}

    def note(folder, filename):
        match = _CFG.match(filename)
        if not match or filename.endswith("~") or filename.lower().endswith(".bak"):
            return
        live = os.path.join(folder, match.group(2))
        found.setdefault(live, []).append(os.path.join(folder, filename))

    for entry in protect:
        path = os.path.join(root, entry.lstrip("/"))
        if os.path.isdir(path):
            for folder, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for filename in files:
                    note(folder, filename)
        else:
            folder, base = os.path.split(path.rstrip("/"))
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for filename in names:
                if filename.endswith("_" + base):
                    note(folder, filename)
    updates = [Update(root, live, files) for live, files in found.items()]
    return sorted((u for u in updates if not _under(u.name, mask or ())), key=lambda u: u.name)


# -- what changed ----------------------------------------------------------------

def read_text(path):
    """A file's text, or None when it can't be read (only root may)"""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def _meaningful(text):
    return [l.rstrip() for l in text.splitlines()
            if l.strip() and not l.lstrip().startswith(("#", ";"))]


def _settings(text):
    """[(section, key, line)] when every line that isn't a comment or blank
    sets a value (shell KEY="value" or ini key = value); else None"""
    found, section = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        match = _SECTION.match(line)
        if match:
            section = match.group(1)
            continue
        match = _SETTING.match(line)
        if not match:
            return None
        value = match.group(2).strip()
        for quote in ('"', "'"):
            if value.startswith(quote) and (len(value) < 2 or not value.endswith(quote)):
                return None     # a value over several lines
        found.append((section, match.group(1), line))
    return found


def merge_settings(live, new, base=None):
    """The new file with your values: (text, notes). A setting you have
    keeps your line; one only the new version has comes with its value;
    one the new version dropped is kept at the end. With the version last
    seen (base), a setting you never changed takes the new default."""
    mine = {(s, k): line for s, k, line in _settings(live) or ()}
    theirs = {(s, k): line for s, k, line in _settings(new) or ()}
    before = {(s, k): line for s, k, line in _settings(base) or ()} if base else {}
    notes, out, section = [], [], None
    for line in new.splitlines():
        match = _SECTION.match(line)
        if match:
            section = match.group(1)
        setting = _SETTING.match(line) if not line.lstrip().startswith(("#", ";")) else None
        key = (section, setting.group(1)) if setting else None
        if key and key in mine:
            if mine[key].strip() == line.strip():
                out.append(line)
            elif key in before and before[key].strip() == mine[key].strip():
                out.append(line)
                notes.append("%s: new default (you hadn't changed it)" % key[1])
            else:
                out.append(mine[key])
                notes.append("%s: your value kept" % key[1])
        else:
            out.append(line)
            if key:
                notes.append("%s: new setting, %s" % (key[1], line.strip()))
    dropped = [k for k in mine if k not in theirs]
    if dropped:
        out += ["", "# Kept from your previous file (the new version doesn't have these):"]
        for key in dropped:
            notes.append("%s: no longer in the new version, kept at the end" % key[1])
            out.append(mine[key])
    return "\n".join(out) + ("\n" if new.endswith("\n") else ""), notes


def merge3(live, base, new):
    """(text, conflicts): your changes and the package's since base merged
    (diff3 -m, from diffutils, which every Gentoo system has); where both
    changed the same lines, both are given between <<<<<<< and >>>>>>>"""
    with tempfile.TemporaryDirectory() as folder:
        paths = []
        for name, text in (("yours", live), ("before", base), ("new", new)):
            path = os.path.join(folder, name)
            with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(text)
            paths.append(path)
        result = subprocess.run(["diff3", "-m", "-L", "yours", "-L", "before",
                                 "-L", "new version"] + paths, capture_output=True)
    if result.returncode not in (0, 1):
        raise ConfigUpdateError("diff3 failed: %s" % result.stderr.decode(errors="replace"))
    text = result.stdout.decode("utf-8", errors="surrogateescape")
    return text, sum(1 for line in text.splitlines() if line.startswith("<<<<<<< "))


def diff(live, new, name="file"):
    return "".join(difflib.unified_diff(live.splitlines(True), new.splitlines(True),
                                        "%s (yours)" % name, "%s (new version)" % name))


def archive_path(target, name, archive_dir=None):
    return os.path.join(archive_dir or ARCHIVE_DIR, target.name, name.lstrip("/"))


def classify(target, update, archive_dir=None):
    """{"kind", "action" (what to do by itself, or None), "result" (text to
    save for "merged"/"settings"/"conflict", else None), "notes" [str],
    "live", "new" (texts or None)}"""
    live, new = read_text(update.live), read_text(update.newest)
    out = {"kind": None, "action": None, "result": None, "notes": [], "live": live,
           "new": new}
    if live is None or new is None:
        out["kind"] = "unreadable"
        return out
    if not os.path.exists(update.live):
        out["kind"] = "differs"
        out["notes"].append("Your file was removed; the new version would bring it back.")
        return out
    if live == new:
        out["kind"] = "same"
    elif _meaningful(live) == _meaningful(new):
        out["kind"] = "comments"
    else:
        base = read_text(archive_path(target, update.name, archive_dir))
        base = base or None
        if base is not None and live == base:
            out["kind"] = "untouched"
        elif base is not None and new == base:
            out["kind"] = "upstream"
        elif base is not None:
            try:
                text, conflicts = merge3(live, base, new)
            except (ConfigUpdateError, OSError) as e:
                # couldn't merge: shown for you to decide
                text, conflicts = None, None
                out["notes"].append("Couldn't merge: %s" % e)
            out["result"] = text
            out["kind"] = ("differs" if conflicts is None else
                           "conflict" if conflicts else "merged")
            if conflicts:
                out["notes"].append("%d place%s changed by both you and the package"
                                    % (conflicts, "" if conflicts == 1 else "s"))
        elif _settings(live) is not None and _settings(new) is not None:
            out["kind"] = "settings"
            out["result"], out["notes"] = merge_settings(live, new)
        else:
            out["kind"] = "differs"
    out["action"] = AUTOMATIC.get(out["kind"])
    return out


# -- settling one (as root) ---------------------------------------------------------

def _save_base(target, name, text, mode, archive_dir=None):
    """The package's version, for the next three-way merge; readable like
    the file itself (some hold passwords)"""
    path = archive_path(target, name, archive_dir)
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)
    os.chmod(path + ".new", mode & 0o644)
    os.replace(path + ".new", path)


def apply(target, name, action, content=None, archive_dir=None, log=print):
    """Settle one file: keep yours, take the new version, or save content
    (a merge, or your own edit). Only for a file with an update waiting."""
    if action not in ACTIONS:
        raise ConfigUpdateError("No such choice: %s" % action)
    found = {u.name: u for u in pending(target)}
    update = found.get(name)
    if update is None:
        raise ConfigUpdateError("%s has no update waiting" % name)
    new = read_text(update.newest)
    new_stat = os.stat(update.newest)
    if action == "new":
        os.replace(update.newest, update.live)
        log("%s: the new version" % name)
    elif action == "merged":
        if content is None:
            raise ConfigUpdateError("Nothing to save for %s" % name)
        reference = update.live if os.path.exists(update.live) else update.newest
        st = os.stat(reference)
        folder = os.path.dirname(update.live)
        fd, tmp = tempfile.mkstemp(prefix=".porthole-", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(content)
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o7777)
            os.replace(tmp, update.live)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        log("%s: merged" % name)
    else:
        log("%s: yours kept" % name)
    for path in update.updates:
        if os.path.exists(path):
            os.unlink(path)
    if new is not None:
        _save_base(target, name, new, new_stat.st_mode, archive_dir)


def auto(target, archive_dir=None, log=print):
    """Settle every update that needs no decision; ([done], [left]) names"""
    done, left = [], []
    for update in pending(target):
        found = classify(target, update, archive_dir)
        if found["action"]:
            try:
                apply(target, update.name, found["action"], found["result"], archive_dir, log)
                done.append(update.name)
            except (OSError, ConfigUpdateError) as e:
                log("!! %s: %s" % (update.name, e))
                left.append(update.name)
        else:
            left.append(update.name)
    return done, left


def report(target, done, left):
    """One line for a job log"""
    if not done and not left:
        return None
    parts = []
    if done:
        parts.append("%d settled by themselves (%s)" % (len(done), ", ".join(done)))
    if left:
        parts.append("%d need you: %s (Porthole: Actions, Configuration Updates)"
                     % (len(left), ", ".join(left)))
    return "Configuration file updates%s: %s" % (
        "" if target.local else " in " + target.title, "; ".join(parts))


# -- the window ------------------------------------------------------------------

KIND_TEXT = {
    "same": "The new version is the same as yours",
    "comments": "Only comments differ: the new version can be taken",
    "untouched": "You hadn't changed it: the new version can be taken",
    "upstream": "The package didn't change it: yours can be kept",
    "merged": "Both changed it in different places: merged",
    "conflict": "Both changed the same lines: you decide",
    "settings": "Settings: the new file with your values",
    "differs": "Different: you decide",
    "unreadable": "Only root can read it",
}


def window():
    """The configuration updates window (made on first use, then reused)"""
    global _window
    if _window is None:
        _window = ConfigUpdatesWindow()
    _window.refresh()
    _window.present()
    return _window


_window = None

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Pango
except (ImportError, ValueError):  # the service imports this module too
    Gtk = None

if Gtk is not None:
    class ConfigUpdatesWindow(Gtk.Window):
        def __init__(self):
            super().__init__(title="Porthole: Configuration Updates")
            from porthole.updater.commands import icon_path
            if icon_path():
                self.set_icon_from_file(icon_path())
            self.set_default_size(980, 600)
            self.connect("delete-event", lambda *a: self.hide() or True)
            self.found = {}     # file name: classify() result
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
            top = Gtk.Box(spacing=8)
            top.pack_start(Gtk.Label(label="For:"), False, False, 0)
            self.targets = Gtk.ComboBoxText()
            self.targets.connect("changed", lambda c: self.refresh())
            top.pack_start(self.targets, False, False, 0)
            refresh = Gtk.Button(label="Look again")
            refresh.connect("clicked", lambda b: self.refresh())
            top.pack_start(refresh, False, False, 0)
            self.auto_button = Gtk.Button(label="Settle the ones that need no decision")
            self.auto_button.connect("clicked", lambda b: self.settle_automatic())
            top.pack_end(self.auto_button, False, False, 0)
            box.pack_start(top, False, False, 0)
            self.heading = Gtk.Label(xalign=0, wrap=True)
            box.pack_start(self.heading, False, False, 0)

            paned = Gtk.Paned(position=360)
            # file, what happened
            self.store = Gtk.ListStore(str, str)
            self.view = Gtk.TreeView(model=self.store)
            for i, title in ((0, "File"), (1, "What happened")):
                cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END if i else
                                            Pango.EllipsizeMode.NONE)
                column = Gtk.TreeViewColumn(title, cell, text=i)
                column.set_resizable(True)
                column.set_expand(bool(i))
                self.view.append_column(column)
            self.view.get_selection().connect("changed", lambda s: self.show_selected())
            scroller = Gtk.ScrolledWindow()
            scroller.add(self.view)
            paned.pack1(scroller, True, False)

            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self.notes = Gtk.Label(xalign=0, wrap=True, selectable=True)
            right.pack_start(self.notes, False, False, 0)
            switch = Gtk.Box(spacing=6)
            self.show_diff = Gtk.RadioButton.new_with_label(None, "Differences")
            self.show_result = Gtk.RadioButton.new_with_label_from_widget(
                self.show_diff, "Result to save (you can edit it)")
            for button in (self.show_diff, self.show_result):
                button.connect("toggled", lambda b: b.get_active() and self.show_selected())
                switch.pack_start(button, False, False, 0)
            right.pack_start(switch, False, False, 0)
            self.text = Gtk.TextView(monospace=True, left_margin=6, right_margin=6)
            buf = self.text.get_buffer()
            buf.create_tag("added", foreground="#1a7f37")
            buf.create_tag("removed", foreground="#cf222e")
            buf.create_tag("hunk", foreground="#8250df")
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.add(self.text)
            right.pack_start(scroller, True, True, 0)
            buttons = Gtk.Box(spacing=8)
            self.keep_button = Gtk.Button(label="Keep mine")
            self.keep_button.connect("clicked", lambda b: self.settle("keep"))
            self.new_button = Gtk.Button(label="Use the new version")
            self.new_button.connect("clicked", lambda b: self.settle("new"))
            self.save_button = Gtk.Button(label="Save this result")
            self.save_button.connect("clicked", lambda b: self.settle("merged"))
            for button in (self.keep_button, self.new_button, self.save_button):
                buttons.pack_start(button, False, False, 0)
            self.message = Gtk.Label(xalign=0, wrap=True)
            buttons.pack_start(self.message, True, True, 0)
            right.pack_start(buttons, False, False, 0)
            paned.pack2(right, True, False)
            box.pack_start(paned, True, True, 0)
            self.add(box)
            self.fill_targets()
            box.show_all()

        def fill_targets(self):
            from porthole.updater import targets
            self.target_list = list(targets.load().values())
            for target in self.target_list:
                self.targets.append_text(target.title)
            self.targets.set_active(0)

        def target(self):
            index = self.targets.get_active()
            return self.target_list[index] if 0 <= index < len(self.target_list) else None

        def refresh(self):
            target = self.target()
            self.store.clear()
            self.found = {}
            self.message.set_text("")
            if target is None:
                return
            try:
                updates = pending(target)
            except Exception as e:
                self.heading.set_text("Couldn't look for them: %s" % e)
                return
            self.updates = {u.name: u for u in updates}
            for update in updates:
                found = classify(target, update)
                self.found[update.name] = found
                self.store.append([update.name, KIND_TEXT.get(found["kind"], found["kind"])])
            automatic = sum(1 for f in self.found.values() if f["action"])
            self.auto_button.set_sensitive(bool(automatic))
            if not updates:
                self.heading.set_text("No configuration file has a new version waiting.")
            else:
                self.heading.set_text(
                    "%d configuration file%s with a package's new version waiting; %d need%s "
                    "no decision. Packages never overwrite a file you changed: their new "
                    "version waits here until you choose." % (
                        len(updates), "" if len(updates) == 1 else "s", automatic,
                        "s" if automatic == 1 else ""))
                self.view.get_selection().select_path(Gtk.TreePath.new_first())
            self.show_selected()

        def selected(self):
            model, it = self.view.get_selection().get_selected()
            return model[it][0] if it else None

        def show_selected(self):
            name = self.selected()
            found = self.found.get(name)
            buf = self.text.get_buffer()
            buf.set_text("")
            for button in (self.keep_button, self.new_button, self.save_button):
                button.set_sensitive(bool(found))
            if not found:
                self.notes.set_text("")
                return
            notes = [KIND_TEXT.get(found["kind"], "")] + found["notes"]
            if len(self.updates[name].updates) > 1:
                notes.append("%d versions waiting; the newest is shown"
                             % len(self.updates[name].updates))
            self.notes.set_text("\n".join(n for n in notes if n))
            has_result = found["kind"] != "unreadable"
            self.show_result.set_sensitive(has_result)
            self.save_button.set_sensitive(has_result)
            if found["kind"] == "unreadable":
                buf.set_text("Only root can read this file, so it can't be shown here. Keep "
                             "yours or take the new version; or look at it in a root "
                             "terminal:\n  diff -u %s %s" % (self.updates[name].live,
                                                             self.updates[name].newest))
                return
            if self.show_result.get_active():
                buf.set_text(found["result"] if found["result"] is not None else
                             found["new"] if found["action"] == "new" else found["live"])
                self.text.set_editable(True)
                return
            self.text.set_editable(False)
            for line in diff(found["live"], found["new"], name).splitlines(True):
                tag = ("hunk" if line.startswith("@@") else
                       "added" if line.startswith("+") and not line.startswith("+++") else
                       "removed" if line.startswith("-") and not line.startswith("---")
                       else None)
                end = buf.get_end_iter()
                if tag:
                    buf.insert_with_tags_by_name(end, line, tag)
                else:
                    buf.insert(end, line)

        def settle(self, choice):
            import os
            import tempfile
            from porthole.updater.elevate import run_as_root, service
            name, target = self.selected(), self.target()
            if not name or target is None:
                return
            argv = ["config-updates", "apply", "--target", target.name, "--file", name]
            path = None
            if choice == "merged":
                if not self.show_result.get_active():
                    self.show_result.set_active(True)
                    self.message.set_text("Check the result, then save it.")
                    return
                buf = self.text.get_buffer()
                text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
                if "<<<<<<<" in text and ">>>>>>>" in text:
                    self.message.set_text("Choose between the <<<<<<< and >>>>>>> lines "
                                          "first (and remove the markers).")
                    return
                cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or
                                     os.path.expanduser("~/.cache"), "porthole")
                os.makedirs(cache, mode=0o700, exist_ok=True)
                fd, path = tempfile.mkstemp(prefix="config-", dir=cache)
                with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
                    f.write(text)
                argv += ["--choice", "merged", "--content", path]
            else:
                argv += ["--choice", choice]

            def done(ok, output):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                self.refresh()
                self.message.set_text("" if ok else "Not saved: %s" % (output or "cancelled"))
            run_as_root(service(*argv), done)

        def settle_automatic(self):
            from porthole.updater.elevate import run_as_root, service
            target = self.target()
            if target is None:
                return

            def done(ok, output):
                self.refresh()
                self.message.set_text((output or "").strip() if ok else
                                      "Not done: %s" % (output or "cancelled"))
            run_as_root(service("config-updates", "auto", "--target", target.name), done)
