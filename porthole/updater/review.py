"""
    Porthole update assistant window

    Shows the plan made by the update service inside Porthole: what will
    be updated, which config changes need approval and why, and the
    stages it runs in. "Apply and start update" writes the approved
    lines to /etc/portage/package.*/zz-porthole and queues the stages in
    Porthole's terminal, pausing the queue if a stage fails. The plan is
    re-checked whenever the queue stops, failed or not.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import datetime
import os
import shlex
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole.updater.analyze import (
    CONFIG_DIRS, KIND_NAMES, OWNED_NAME, TEMP_NAME, format_report, summarize)
from porthole.updater.commands import script_command
from porthole.updater.service import PLAN_FILE, last_repo_sync, read_json

PORTAGE_DIR = "/etc/portage"
SENDER = "Update Assistant"
# changes this close to the check were made by the check itself
GRACE = 60


class ConfigError(Exception):
    pass


def plan_time(plan):
    try:
        return datetime.datetime.fromisoformat(plan["generated"]).timestamp()
    except (KeyError, ValueError):
        return 0


def newest_config_change(top=PORTAGE_DIR):
    newest = 0
    for dirpath, dirnames, filenames in os.walk(top):
        for name in dirnames + filenames:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                pass
    return newest


def stale_reason(plan):
    """Why the plan may no longer match the system, or None"""
    checked = plan_time(plan)
    if (last_repo_sync() or 0) > checked + GRACE:
        return "The repositories were synced after this check."
    if newest_config_change() > checked + GRACE:
        return "/etc/portage was changed after this check."
    return None


def write_fixes(fixes):
    """Append approved lines to /etc/portage/package.*/zz-porthole"""
    for fix in fixes:
        path = os.path.join(PORTAGE_DIR, CONFIG_DIRS[fix["kind"]])
        if os.path.isfile(path):
            raise ConfigError(
                "%s is a single file. Porthole keeps its changes in its own file, "
                "so it needs %s to be a directory. Move the file into a new "
                "directory of that name and re-check." % (path, path))
    stamp = time.strftime("%Y-%m-%d")
    written = []
    for fix in fixes:
        directory = os.path.join(PORTAGE_DIR, CONFIG_DIRS[fix["kind"]])
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, OWNED_NAME)
        try:
            with open(path) as f:
                existing = f.read().splitlines()
        except FileNotFoundError:
            existing = None
        if existing and fix["line"] in existing:
            continue
        with open(path, "a") as f:
            if existing is None:
                f.write("# Added by Porthole's update assistant. "
                        "Safe to edit or remove.\n")
            reason = (fix.get("explanation") or [""])[0]
            f.write("\n# %s: %s\n%s\n" % (stamp, reason, fix["line"]))
        written.append((path, fix["line"]))
    return written


def stage_shell(stage):
    """One shell command line for a stage; stops at the first failure"""
    temp = os.path.join(PORTAGE_DIR, CONFIG_DIRS["use"], TEMP_NAME)
    steps = []
    if stage.get("remove_config"):
        steps.append("rm -f " + shlex.quote(temp))
    if stage.get("config"):
        steps.append("printf '%s\\n' " + " ".join(shlex.quote(line) for line in
                     stage["config"]["use"]) + " > " + shlex.quote(temp))
    steps.extend(shlex.join(command) for command in stage["commands"])
    # pick up a compiler switched by an earlier stage
    return ". /etc/profile >/dev/null 2>&1; " + " && ".join(steps)


def _label(text, mono=False, bold=False, markup=None, selectable=False):
    label = Gtk.Label(xalign=0, wrap=True, selectable=selectable)
    if markup is not None:
        label.set_markup(markup)
    else:
        text = GLib.markup_escape_text(text)
        if mono:
            text = "<tt>%s</tt>" % text
        if bold:
            text = "<b>%s</b>" % text
        label.set_markup(text)
    return label


def _section(title):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.pack_start(_label("", markup="<b>%s</b>" % GLib.markup_escape_text(title)),
                   False, False, 0)
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12)
    box.pack_start(inner, False, False, 0)
    return box, inner


class UpdateAssistant(Gtk.Window):
    def __init__(self, parent, mainwindow):
        super().__init__(title="Porthole: update assistant")
        self.set_transient_for(parent)
        self.set_default_size(860, 700)
        self.mainwindow = mainwindow
        self.plan = None
        self.connect("delete-event", lambda *a: self.hide() or True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
        self.infobar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING)
        self.infobar_label = _label("")
        self.infobar.get_content_area().add(self.infobar_label)
        outer.pack_start(self.infobar, False, False, 0)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                               margin_end=8)
        scroller.add(self.content)
        outer.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        self.news_button = Gtk.Button(label="Read news")
        self.news_button.connect("clicked", self.on_news)
        self.recheck_button = Gtk.Button(label="Re-check")
        self.recheck_button.connect("clicked", self.on_recheck)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.hide())
        self.start_button = Gtk.Button(label="Apply and start update")
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.connect("clicked", self.on_start)
        buttons.pack_start(self.news_button, False, False, 0)
        buttons.pack_start(self.recheck_button, False, False, 0)
        buttons.pack_end(self.start_button, False, False, 0)
        buttons.pack_end(close, False, False, 0)
        outer.pack_start(buttons, False, False, 0)
        self.add(outer)

    # -- display -----------------------------------------------------------

    def load(self, *args):
        self.plan = read_json(PLAN_FILE)
        for child in self.content.get_children():
            child.destroy()
        plan = self.plan
        if not plan:
            self.content.pack_start(_label(
                "", markup="<big><b>No update check has run yet</b></big>"), False, False, 0)
            self.content.pack_start(_label(
                "Press Re-check to work out what can be updated."), False, False, 0)
            self.show_buttons(False, None)
            self.show_all()
            self.infobar.hide()
            return False

        title, body = summarize(plan)
        checked = plan.get("generated", "").replace("T", " ")[:16]
        self.content.pack_start(_label("", markup="<big><b>%s</b></big>"
                                       % GLib.markup_escape_text(title)), False, False, 0)
        self.content.pack_start(_label("%s\nChecked %s." % (body, checked)), False, False, 0)

        status = plan.get("status")
        self._checks(plan)
        if status == "needs-help" or status == "error":
            box, inner = _section("Why it can't be updated automatically")
            inner.pack_start(self._report_view(plan), False, False, 0)
            self.content.pack_start(box, False, False, 0)
        else:
            self._approvals(plan)
            self._stages(plan)
            self._notes(plan)
        self._packages(plan)

        stale = stale_reason(plan)
        self.show_buttons(status in ("ready", "needs-approval"), stale)
        self.show_all()
        if stale:
            self.infobar_label.set_text(stale + " Re-check before updating.")
            self.infobar.show()
        else:
            self.infobar.hide()
        return False

    def _checks(self, plan):
        checks = plan.get("checks") or []
        if not checks:
            return
        box, inner = _section("Problems on this system")
        for check in checks:
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            item.pack_start(_label("%s%s" % ("" if check["severity"] == "error" else "Warning: ",
                                             check["title"]), bold=True), False, False, 0)
            item.pack_start(_label(check["detail"], selectable=True), False, False, 0)
            item.pack_start(_label("Fix: " + check["fix"], selectable=True), False, False, 0)
            inner.pack_start(item, False, False, 0)
        self.content.pack_start(box, False, False, 0)

    def _approvals(self, plan):
        fixes = plan.get("fixes") or []
        approvals = [s for s in plan.get("stages") or [] if s.get("approval")]
        if not fixes and not approvals:
            return
        box, inner = _section("Changes that need your approval")
        for kind in CONFIG_DIRS:
            kind_fixes = [f for f in fixes if f["kind"] == kind]
            if not kind_fixes:
                continue
            inner.pack_start(_label("Add to %s/%s/%s:" % (PORTAGE_DIR, CONFIG_DIRS[kind],
                                                         OWNED_NAME)), False, False, 0)
            for fix in kind_fixes:
                item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=12)
                item.pack_start(_label(fix["line"], mono=True, bold=True, selectable=True),
                                False, False, 0)
                for line in fix.get("explanation") or []:
                    item.pack_start(_label(line, selectable=True), False, False, 0)
                inner.pack_start(item, False, False, 0)
        for stage in approvals:
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            item.pack_start(_label(stage["title"], bold=True), False, False, 0)
            item.pack_start(_label(stage.get("why", "")), False, False, 0)
            inner.pack_start(item, False, False, 0)
        for cycle in plan.get("cycles_broken") or []:
            inner.pack_start(_label(
                "Temporarily %s to break the circular dependency %s"
                % (cycle["temp_use"], " → ".join(cycle["cycle"]))), False, False, 0)
        self.content.pack_start(box, False, False, 0)

    def _stages(self, plan):
        box, inner = _section("What happens")
        for i, stage in enumerate(plan.get("stages") or [], 1):
            inner.pack_start(_label("%d. %s" % (i, stage["title"]), bold=True), False, False, 0)
            if stage.get("config"):
                inner.pack_start(_label("Temporary: " + ", ".join(stage["config"]["use"]),
                                        mono=True, selectable=True), False, False, 0)
            for command in stage["commands"]:
                inner.pack_start(_label("$ " + shlex.join(command), mono=True,
                                        selectable=True), False, False, 0)
        inner.pack_start(_label("If a step fails, the remaining steps wait until you "
                                "resume them in the terminal window's queue."), False, False, 0)
        self.content.pack_start(box, False, False, 0)

    def _notes(self, plan):
        notes = list(plan.get("notes") or [])
        full = plan.get("full_rebuild")
        if full:
            notes.append("Optional full rebuild: %s Run it later with: %s"
                         % (full["reason"], shlex.join(full["commands"][0])))
        if not notes:
            return
        box, inner = _section("Notes")
        for note in notes:
            inner.pack_start(_label(note, selectable=True), False, False, 0)
        self.content.pack_start(box, False, False, 0)

    def _packages(self, plan):
        merges = plan.get("merge_list") or []
        rebuilds = (plan.get("preserved") or {}).get("merge_list") or []
        if not merges and not rebuilds:
            return
        store = Gtk.ListStore(str, str, str)
        for m in merges:
            store.append([KIND_NAMES.get(m["kind"], "?"), m["cpv"], ", ".join(m["replaces"])])
        for m in rebuilds:
            store.append([KIND_NAMES.get(m["kind"], "?"), m["cpv"], "uses old libraries"])
        view = Gtk.TreeView(model=store)
        for i, title in enumerate(("", "Package", "Replaces")):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        scroller = Gtk.ScrolledWindow(min_content_height=260)
        scroller.add(view)
        expander = Gtk.Expander(label="All %d packages" % (len(merges) + len(rebuilds)))
        expander.add(scroller)
        self.content.pack_start(expander, False, False, 0)

    def _report_view(self, plan):
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(format_report(plan))
        return view

    def show_buttons(self, can_start, stale):
        news = (self.plan or {}).get("news_unread")
        self.news_button.set_label("Read news (%d)" % news if news else "Read news")
        self.news_button.set_sensitive(bool(news))
        is_root = os.geteuid() == 0
        self.start_button.set_sensitive(is_root and can_start and not stale)
        self.recheck_button.set_sensitive(is_root)
        if not is_root:
            self.start_button.set_tooltip_text("Porthole must run as root to update")
        elif stale:
            self.start_button.set_tooltip_text("Re-check first: the plan may be out of date")
        else:
            self.start_button.set_tooltip_text(None)

    # -- actions -------------------------------------------------------------

    @property
    def process_manager(self):
        return self.mainwindow.process_manager

    def queue(self, name, command, callback=None):
        self.process_manager.add(name, command, callback or (lambda: None), SENDER)

    def on_news(self, button):
        self.queue("Read Gentoo news", "eselect news read new")

    def on_recheck(self, button):
        self.recheck_button.set_sensitive(False)
        self.start_button.set_sensitive(False)
        self.queue("Check for updates",
                   shlex.join(script_command("porthole-update-service")
                              + ["run", "--force"]),
                   lambda: GLib.idle_add(self.load))

    def on_start(self, button):
        plan = self.plan
        fixes = plan.get("fixes") or []
        stages = plan.get("stages") or []
        text = "Start the update?"
        details = ["%d packages will be built. Progress shows in Porthole's terminal window."
                   % len(plan.get("merge_list") or [])]
        if fixes:
            details.insert(0, "These lines will be added to /etc/portage:\n"
                           + "\n".join("  " + f["line"] for f in fixes))
        if any(s.get("approval") for s in stages):
            details.append("This includes: " + "; ".join(
                s["title"] for s in stages if s.get("approval")) + ".")
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=text)
        dialog.set_title("Porthole")
        dialog.format_secondary_text("\n\n".join(details))
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Start update", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        try:
            write_fixes(fixes)
        except (ConfigError, OSError) as e:
            self.error("Could not update /etc/portage", str(e))
            return

        for i, stage in enumerate(stages, 1):
            self.queue("Update step %d of %d: %s" % (i, len(stages), stage["title"]),
                       stage_shell(stage),
                       lambda i=i, stage=stage: self.stage_done(i, stage))
        self.queue("Refresh update status",
                   shlex.join(script_command("porthole-update-service")
                              + ["run", "--force", "--no-sync"]),
                   self.all_done)
        self.start_button.set_sensitive(False)
        self.hide()

    def stage_done(self, number, stage):
        status = getattr(self.process_manager, "exit_status", None)
        if status in (0, None):
            return
        # runs before the queue moves on, so pausing here holds the rest
        self.process_manager.process_queue.pause()
        # the queued refresh won't run while paused; the plan, the tray and
        # this window must still show what is left
        self.refresh()
        self.error(
            "Update step %d failed" % number,
            "\"%s\" stopped with exit code %d. The remaining steps are paused.\n\n"
            "Check the terminal output, fix the problem, then press play in the "
            "queue to continue. Or close the queue and use Re-check to work out a "
            "new plan. Porthole is re-checking in the background now, so the update "
            "assistant will show what is left." % (stage["title"], status))

    def refresh(self):
        """Re-check outside the queue, then reload"""
        argv = script_command("porthole-update-service") + ["run", "--force", "--no-sync",
                                                             "--quiet"]
        try:
            pid, *_ = GLib.spawn_async(
                argv, flags=GLib.SpawnFlags.DO_NOT_REAP_CHILD
                | GLib.SpawnFlags.STDOUT_TO_DEV_NULL | GLib.SpawnFlags.STDERR_TO_DEV_NULL)
        except GLib.Error:
            return
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, lambda *a: self.all_done())

    def all_done(self):
        if hasattr(self.mainwindow, "reload_db"):
            self.mainwindow.reload_db()
        GLib.idle_add(self.load)

    def error(self, title, text):
        dialog = Gtk.MessageDialog(transient_for=self.get_transient_for(), modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text=title)
        dialog.set_title("Porthole")
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def present_plan(self):
        self.load()
        self.present()
