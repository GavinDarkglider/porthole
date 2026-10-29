"""
    Porthole update assistant window

    Shows the plan made by the update service inside Porthole: what will
    be updated, which config changes need approval and why, and the
    stages it runs in. "Apply and start update" hands the reviewed plan to
    the root job runner, so this window (and Porthole) can be closed while
    it runs; progress shows in the job window and the tray.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import shlex

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk

from porthole.updater.analyze import (
    CONFIG_DIRS, KIND_NAMES, OWNED_NAME, format_report, normalize_choices, summarize)
from porthole.updater.commands import icon_path
from porthole.updater.elevate import run_as_root, service
from porthole.updater.runner import ACTIVE, PORTAGE_DIR, stale_reason
from porthole.updater.service import PLAN_FILE, STATE_DIR, read_json
from porthole.updater.viewer import read_jobs


def _label(text, mono=False, bold=False, markup=None, selectable=False, **props):
    label = Gtk.Label(xalign=0, wrap=True, selectable=selectable, **props)
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
        # a window of its own, not transient for Porthole's: window managers
        # keep transients above their parent and won't minimize them alone
        if icon_path():
            self.set_icon_from_file(icon_path())
        self.set_default_size(860, 700)
        self.mainwindow = mainwindow
        self.plan = None
        self.connect("delete-event", lambda *a: self.hide() or True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
        self.infobar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING)
        self.infobar_label = _label("")
        self.infobar.get_content_area().add(self.infobar_label)
        outer.pack_start(self.infobar, False, False, 0)

        # shown once the user changes the plan: it has to be worked out
        # (and verified) again before it can run
        self.choices_bar = Gtk.InfoBar(message_type=Gtk.MessageType.INFO)
        self.choices_label = _label("You changed the plan. Work it out again to check "
                                    "your choices before updating.")
        self.choices_bar.get_content_area().add(self.choices_label)
        self.choices_bar.add_button("Undo changes", 1)
        self.choices_bar.add_button("Re-plan with my choices", 2)
        self.choices_bar.connect("response", self.on_choices_response)
        outer.pack_start(self.choices_bar, False, False, 0)
        self.pending = None

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                               margin_end=8)
        scroller.add(self.content)
        outer.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        self.news_button = Gtk.Button(label="Read news")
        self.news_button.connect("clicked", self.on_news)
        self.recheck_button = Gtk.Button(label="Check now")
        self.recheck_button.connect("clicked", self.on_recheck)
        self.progress_button = Gtk.Button(label="Show progress")
        self.progress_button.connect("clicked", lambda b: self.show_progress())
        self.cancel_button = Gtk.Button(label="Cancel update")
        self.cancel_button.connect("clicked", self.on_cancel)
        settings = Gtk.Button(label="Settings")
        settings.connect("clicked", lambda b: self.mainwindow.configure_porthole(page="updates"))
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.hide())
        self.start_button = Gtk.Button(label="Apply and start update")
        self.start_button.get_style_context().add_class("suggested-action")
        self.start_button.connect("clicked", self.on_start)
        buttons.pack_start(self.news_button, False, False, 0)
        buttons.pack_start(self.recheck_button, False, False, 0)
        buttons.pack_start(self.progress_button, False, False, 0)
        buttons.pack_start(self.cancel_button, False, False, 0)
        buttons.pack_start(settings, False, False, 0)
        buttons.pack_end(self.start_button, False, False, 0)
        buttons.pack_end(close, False, False, 0)
        outer.pack_start(buttons, False, False, 0)
        self.add(outer)

        self.job_terminal = None
        self.finished_jobs = self.finished_job_ids()
        # the service replaces its files with a rename, so watch the directory
        self.monitor = None
        try:
            self.monitor = Gio.File.new_for_path(STATE_DIR).monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
            self.monitor.connect("changed", self.on_state_changed)
        except GLib.Error:
            pass
        self.reload_pending = False

    # -- display -----------------------------------------------------------

    def load(self, *args):
        previous = (self.plan or {}).get("generated")
        self.plan = read_json(PLAN_FILE)
        for child in self.content.get_children():
            child.destroy()
        plan = self.plan
        if self.pending is None or (plan or {}).get("generated") != previous:
            # a new plan starts from the choices it was made with
            self.pending = normalize_choices((plan or {}).get("choices"))
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
        if status == "error" or (status == "needs-help" and not plan.get("verified", True)):
            box, inner = _section("Why it can't be updated automatically")
            inner.pack_start(self._report_view(plan), False, False, 0)
            self.content.pack_start(box, False, False, 0)
            self._held_back(plan)
        else:
            self._approvals(plan)
            self._held_back(plan)
            self._stages(plan)
            self._notes(plan)
        self._held_by_you(plan)
        self._packages(plan)

        stale = stale_reason(plan)
        self._jobs()
        self.show_buttons(status in ("ready", "needs-approval"), stale)
        self.show_all()
        self.update_choices_bar()
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
                item.pack_start(self._choice(
                    _label(fix["line"], mono=True, bold=True),
                    "reject_fixes", fix["line"], inverted=True), False, False, 0)
                for line in fix.get("explanation") or []:
                    item.pack_start(_label(line, selectable=True, margin_start=28),
                                    False, False, 0)
                inner.pack_start(item, False, False, 0)
        for stage in approvals:
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = _label(stage["title"], bold=True)
            item.pack_start(self._choice(title, "decline", stage["key"], inverted=True)
                            if stage.get("key") else title, False, False, 0)
            item.pack_start(_label(stage.get("why", ""), margin_start=28), False, False, 0)
            inner.pack_start(item, False, False, 0)
        if fixes or any(s.get("key") for s in approvals):
            inner.pack_start(_label("Untick anything you don't want; the packages that "
                                    "need it are then kept at their current version."),
                             False, False, 0)
        for cycle in plan.get("cycles_broken") or []:
            inner.pack_start(_label(
                "Temporarily %s to break the circular dependency %s"
                % (cycle["temp_use"], " → ".join(cycle["cycle"]))), False, False, 0)
        self.content.pack_start(box, False, False, 0)

    def _held_back(self, plan):
        held = plan.get("held_back") or []
        if not held and not plan.get("missing_world"):
            return
        box, inner = _section("Held back")
        if held:
            inner.pack_start(_label("Portage leaves these out of the update (and still "
                                    "reports success). They need a decision from you."),
                             False, False, 0)
        for h in held:
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=12)
            item.pack_start(_label("%s (staying at %s)" % (h["cpv"], h["selected"] or "nothing"),
                                   bold=True, selectable=True), False, False, 0)
            for line in h["why"]:
                item.pack_start(_label(line, selectable=True), False, False, 0)
            for option in h.get("options") or []:
                kind, _sep, value = option["key"].partition(":")
                if kind == "exclude":
                    item.pack_start(self._choice(_label(option["label"] + " (stop offering "
                                                        "this update)"), "exclude", value),
                                    False, False, 0)
                else:
                    item.pack_start(self._choice(_label(option["label"] + " so the update "
                                                        "can go ahead"),
                                                 "accept", option["key"]), False, False, 0)
            inner.pack_start(item, False, False, 0)
        if plan.get("missing_world"):
            inner.pack_start(_label("In your world file but no repository has them any "
                                    "more: " + ", ".join(plan["missing_world"]),
                                    selectable=True), False, False, 0)
        self.content.pack_start(box, False, False, 0)

    def _held_by_you(self, plan):
        """What the user's choices keep back, each with a way to undo it"""
        held = plan.get("held_by_you") or []
        declined = [k for k in (plan.get("choices") or {}).get("decline") or []
                    if not any(h["undo"] == {"list": "decline", "value": k} for h in held)]
        if not held and not declined:
            return
        box, inner = _section("Kept back by your choices")
        for h in held:
            text = _label("%s: %s" % (h["cp"], h["why"]))
            if h.get("undo"):
                inner.pack_start(self._choice(text, h["undo"]["list"], h["undo"]["value"]),
                                 False, False, 0)
            else:
                # follows from another choice above; undo that one instead
                text.set_margin_start(28)
                inner.pack_start(text, False, False, 0)
        for key in declined:
            kind, _sep, cp = key.partition(":")
            text = {"remove": "Keep %s installed" % cp,
                    "replace": "Keep %s rather than let an update replace it" % cp,
                    "gcc-switch": "Stay on the current GCC"}.get(kind, key)
            inner.pack_start(self._choice(_label(text), "decline", key), False, False, 0)
        inner.pack_start(_label("Untick to offer the update again."), False, False, 0)
        self.content.pack_start(box, False, False, 0)

    # -- choices -------------------------------------------------------------

    def _choice(self, label, key, value, inverted=False):
        """A check box bound to one entry of the pending choices. Plain:
        ticked means the entry is in the list; inverted: ticked means it
        isn't (e.g. a fix is ticked unless rejected)."""
        button = Gtk.CheckButton()
        button.add(label)
        button.set_active((value in self.pending[key]) != inverted)

        def toggled(b):
            wanted = b.get_active() != inverted
            entries = set(self.pending[key])
            entries.add(value) if wanted else entries.discard(value)
            self.pending[key] = sorted(entries)
            self.update_choices_bar()
        button.connect("toggled", toggled)
        return button

    def choices_changed(self):
        return bool(self.plan) and self.pending != normalize_choices(self.plan.get("choices"))

    def update_choices_bar(self):
        changed = self.choices_changed()
        self.choices_bar.set_visible(changed)
        if self.plan:
            self.show_buttons(self.plan.get("status") in ("ready", "needs-approval"),
                              stale_reason(self.plan))

    def on_choices_response(self, bar, response):
        if response == 1:
            self.pending = None
            self.plan = None  # forces the pending choices to be reset
            self.load()
        elif response == 2:
            self.choices_bar.set_sensitive(False)
            self.choices_label.set_text("Working out a new plan with your choices...")
            run_as_root(service("submit", "check", "--no-sync",
                                "--choices", json.dumps(self.pending)), self.replanned)

    def replanned(self, ok, output):
        self.choices_bar.set_sensitive(True)
        self.choices_label.set_text("You changed the plan. Work it out again to check "
                                    "your choices before updating.")
        if not ok:
            self.error("Couldn't re-plan", output or "Cancelled.")
        else:
            self.show_progress()

    def _stages(self, plan):
        if not plan.get("stages"):
            return
        box, inner = _section("What happens")
        for i, stage in enumerate(plan.get("stages") or [], 1):
            inner.pack_start(_label("%d. %s" % (i, stage["title"]), bold=True), False, False, 0)
            if stage.get("config"):
                inner.pack_start(_label("Temporary: " + ", ".join(stage["config"]["use"]),
                                        mono=True, selectable=True), False, False, 0)
            for command in stage["commands"]:
                inner.pack_start(_label("$ " + shlex.join(command), mono=True,
                                        selectable=True), False, False, 0)
        inner.pack_start(_label("The update runs on its own, so you can close Porthole. "
                                "If a step fails the rest are skipped, and the plan is "
                                "checked again to show what is left."), False, False, 0)
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
        lives = (plan.get("live") or {}).get("changed") or []
        if not merges and not rebuilds and not lives:
            return
        # update?, can untick, kind, package, replaces, cp
        store = Gtk.ListStore(bool, bool, str, str, str, str)
        for m in merges:
            store.append([m["cp"] not in self.pending["exclude"], True,
                          KIND_NAMES.get(m["kind"], "?"), m["cpv"], ", ".join(m["replaces"]),
                          m["cp"]])
        for c in lives:
            store.append([c["cp"] not in self.pending["exclude"], True, "L", c["cpv"],
                          "new upstream commits", c["cp"]])
        for m in rebuilds:
            store.append([True, False, KIND_NAMES.get(m["kind"], "?"), m["cpv"],
                          "uses old libraries", m["cp"]])
        view = Gtk.TreeView(model=store)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_package_toggled, store)
        view.append_column(Gtk.TreeViewColumn("Update", toggle, active=0, activatable=1,
                                              sensitive=1))
        for i, title in ((2, ""), (3, "Package"), (4, "Replaces")):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        scroller = Gtk.ScrolledWindow(min_content_height=260)
        scroller.add(view)
        expander = Gtk.Expander(label="All %d packages (untick to keep one at its "
                                      "current version)"
                                % (len(merges) + len(rebuilds) + len(lives)),
                                expanded=True)
        expander.add(scroller)
        self.content.pack_start(expander, False, False, 0)

    def on_package_toggled(self, renderer, path, store):
        row = store[path]
        row[0] = not row[0]
        entries = set(self.pending["exclude"])
        entries.discard(row[5]) if row[0] else entries.add(row[5])
        self.pending["exclude"] = sorted(entries)
        self.update_choices_bar()

    def _report_view(self, plan):
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(format_report(plan))
        return view

    def _jobs(self):
        """A line about the running or last update job"""
        jobs = read_jobs()
        active = [j for j in jobs if j["status"] in ACTIVE]
        last = active[0] if active else (jobs[-1] if jobs else None)
        if not last or (not active and last["status"] == "done"):
            return
        from porthole.updater.viewer import describe
        text = describe(last)
        if last["status"] == "failed":
            text += ". The plan below is what is left."
        box, inner = _section("Update in progress" if active else "Last update")
        inner.pack_start(_label(text), False, False, 0)
        self.content.pack_start(box, False, False, 0)
        self.content.reorder_child(box, 2)

    def show_buttons(self, can_start, stale):
        news = (self.plan or {}).get("news_unread")
        self.news_button.set_label("Read news (%d)" % news if news else "Read news")
        self.news_button.set_sensitive(bool(news))
        jobs = read_jobs()
        busy = any(j["status"] in ACTIVE for j in jobs)
        changed = self.choices_changed()
        self.start_button.set_sensitive(can_start and not stale and not busy and not changed)
        self.recheck_button.set_sensitive(not busy)
        self.progress_button.set_visible(bool(jobs))
        self.cancel_button.set_visible(any(j["status"] in ACTIVE and j["kind"] == "update"
                                           for j in jobs))
        if busy:
            self.start_button.set_tooltip_text("An update or check is running")
        elif changed:
            self.start_button.set_tooltip_text("Re-plan with your choices first")
        elif stale:
            self.start_button.set_tooltip_text("Check again first: the plan may be out of date")
        else:
            self.start_button.set_tooltip_text(None)

    # -- actions -------------------------------------------------------------

    @property
    def process_manager(self):
        return self.mainwindow.process_manager

    def on_news(self, button):
        self.process_manager.add("Read Gentoo news", "eselect news read new",
                                 lambda: None, "Update Assistant")

    def on_recheck(self, button):
        self.recheck_button.set_sensitive(False)
        run_as_root(service("submit", "check"), self.submitted)

    def on_start(self, button):
        plan = self.plan
        fixes = plan.get("fixes") or []
        stages = plan.get("stages") or []
        details = ["The update runs in the background as root. You can close Porthole; "
                   "progress shows in the update window and the tray."]
        if fixes:
            details.insert(0, "These lines will be added to /etc/portage:\n"
                           + "\n".join("  " + f["line"] for f in fixes))
        if any(s.get("approval") for s in stages):
            details.append("This includes: " + "; ".join(
                s["title"] for s in stages if s.get("approval")) + ".")
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text="Start the update?")
        dialog.set_title("Porthole")
        dialog.format_secondary_text("\n\n".join(details))
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Start update", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.start_button.set_sensitive(False)
        run_as_root(service("submit", "update", "--plan", plan["generated"]),
                    lambda ok, out: self.submitted(ok, out, show=True))

    def submitted(self, ok, output, show=False):
        if not ok:
            self.error("Couldn't start", output or "Cancelled.")
            self.load()
            return
        if show:
            self.show_progress()
        self.load()

    def show_progress(self):
        """Follow the job in a Porthole terminal (tabs, colours, summary)"""
        if self.job_terminal is not None and self.job_terminal.window_visible:
            self.job_terminal.window.present()
            return
        from porthole.updater.follow import show_in_terminal
        self.job_terminal = show_in_terminal()

    def on_cancel(self, button):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.YES_NO, text="Stop the update?")
        dialog.format_secondary_text(
            "The package being built is stopped; packages already installed stay "
            "installed. Porthole then checks what is left, so you can start the rest "
            "later.")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            button.set_sensitive(False)
            run_as_root(service("cancel"), lambda ok, out: (
                button.set_sensitive(True), None if ok else self.error(
                    "Couldn't cancel", out or "Cancelled."), self.load()))

    def finished_job_ids(self):
        return {j["id"] for j in read_jobs() if j["status"] not in ACTIVE}

    def on_state_changed(self, monitor, file, other, event):
        if file.get_basename() in ("plan.json", "jobs.json") and not self.reload_pending:
            self.reload_pending = True
            GLib.timeout_add(500, self.state_changed)

    def state_changed(self):
        self.reload_pending = False
        finished = self.finished_job_ids()
        if finished - self.finished_jobs:
            # packages changed: Porthole's package lists are out of date
            if hasattr(self.mainwindow, "reload_db"):
                self.mainwindow.reload_db()
        self.finished_jobs = finished
        if self.get_visible():
            self.load()
        return False

    def error(self, title, text):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text=title)
        dialog.set_title("Porthole")
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def present_plan(self):
        self.load()
        self.present()
