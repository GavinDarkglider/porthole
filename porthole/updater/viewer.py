"""
    Porthole update assistant: watching update jobs

    A window that follows the job runner: the job's steps, emerge's
    progress and the log as it is written. It only reads; closing it
    doesn't affect the job, which runs as root on its own.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import re
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole.updater.commands import icon_path
from porthole.updater.elevate import run_as_root, service
from porthole.updater.runner import ACTIVE, JOBS_FILE
from porthole.updater.service import read_json

STATUS_WORDS = {"queued": "Waiting", "running": "Running", "done": "Finished",
                "failed": "Failed", "cancelled": "Cancelled", "not run": "Not run"}
STEP_MARKS = {"queued": "•", "running": "▶", "done": "✓",
              "failed": "✗", "cancelled": "✗", "not run": "–"}
# terminal escapes and carriage-return progress bars
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# read at most this much of a log when opening it
TAIL_BYTES = 512 * 1024


def read_jobs():
    return (read_json(JOBS_FILE, {}) or {}).get("jobs", [])


def current_job(jobs=None):
    """The running job, else the next queued one, else the last finished"""
    jobs = read_jobs() if jobs is None else jobs
    for status in ("running", "queued"):
        for job in jobs:
            if job["status"] == status:
                return job
    return jobs[-1] if jobs else None


def describe(job):
    """One line about a job, e.g. for a tooltip"""
    if not job:
        return ""
    progress = job.get("progress") or {}
    if job["status"] == "running" and job.get("rechecking"):
        return "%s: checking what is left" % job["title"]
    if job["status"] == "running" and progress:
        return "%s: %d of %d done, building %s" % (
            job["title"], progress["done"], progress["total"], progress["current"])
    if job["status"] == "running":
        step = next((s for s in job["steps"] if s["status"] == "running"), None)
        return "%s: %s" % (job["title"], step["title"] if step else "running")
    return "%s: %s" % (job["title"], STATUS_WORDS.get(job["status"], job["status"]).lower())


def clean(text):
    text = _ESCAPES.sub("", text)
    # keep only what a terminal would show after carriage returns
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


class JobWindow(Gtk.Window):
    def __init__(self, application=None):
        super().__init__(title="Porthole: update progress")
        if application:
            self.set_application(application)
        self.set_default_size(860, 620)
        if icon_path():
            self.set_icon_from_file(icon_path())
        self.connect("delete-event", lambda *a: self.hide() or True)
        self.job_id = None
        self.log_path = None
        self.offset = 0
        self.timer = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, border_width=10)
        self.heading = Gtk.Label(xalign=0, selectable=True)
        box.pack_start(self.heading, False, False, 0)
        self.progress = Gtk.ProgressBar(show_text=True)
        box.pack_start(self.progress, False, False, 0)
        self.steps = Gtk.Label(xalign=0, yalign=0, selectable=True)
        box.pack_start(self.steps, False, False, 0)

        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.CHAR)
        buf = self.view.get_buffer()
        self.end_mark = buf.create_mark("end", buf.get_end_iter(), False)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.add(self.view)
        box.pack_start(self.scroller, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        note = Gtk.Label(xalign=0)
        note.set_markup("<small>Closing this window doesn't stop the update.</small>")
        buttons.pack_start(note, True, True, 0)
        self.cancel_button = Gtk.Button(label="Cancel update")
        self.cancel_button.connect("clicked", self.on_cancel)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.hide())
        buttons.pack_end(close, False, False, 0)
        buttons.pack_end(self.cancel_button, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self.connect("show", lambda w: self.start())
        self.connect("hide", lambda w: self.stop())

    def present_job(self, job_id=None):
        """Show a job (default: the current one)"""
        self.job_id = job_id
        self.show_all()
        self.present()
        self.refresh()

    def start(self):
        if self.timer is None:
            self.timer = GLib.timeout_add(700, self.refresh)

    def stop(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None

    def refresh(self):
        jobs = read_jobs()
        job = next((j for j in jobs if j["id"] == self.job_id), None) if self.job_id else None
        # follow the queue unless a particular job was asked for
        job = job or current_job(jobs)
        if not job:
            self.heading.set_markup("<b>No update jobs yet</b>")
            self.cancel_button.hide()
            return True
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(job["submitted"]))
        self.heading.set_markup("<b>%s</b>  —  %s  (started %s by %s)" % (
            GLib.markup_escape_text(job["title"]),
            STATUS_WORDS.get(job["status"], job["status"]), when,
            GLib.markup_escape_text(job.get("by", "?"))))
        self.steps.set_text("\n".join(
            "%s %s" % (STEP_MARKS.get(s["status"], "?"), s["title"]) for s in job["steps"])
            + ("\n▶ Checking what is left to update" if job.get("rechecking") else ""))
        progress = job.get("progress") or {}
        if job["status"] == "running" and progress:
            self.progress.set_fraction(progress["done"] / max(1, progress["total"]))
            self.progress.set_text("%d of %d — %s" % (
                progress["done"], progress["total"], progress["current"]))
            self.progress.show()
        elif job["status"] == "running":
            self.progress.pulse()
            self.progress.set_text(describe(job))
            self.progress.show()
        else:
            self.progress.hide()
        self.cancel_button.set_visible(job["status"] in ACTIVE)
        self.cancel_button.set_label("Cancel update" if job["kind"] == "update"
                                     else "Cancel")
        self.follow_log(job["log"])
        return True

    def follow_log(self, path):
        buf = self.view.get_buffer()
        if path != self.log_path:
            self.log_path, self.offset = path, 0
            buf.set_text("")
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size > TAIL_BYTES:
                self.offset = size - TAIL_BYTES
                buf.set_text("(earlier output not shown)\n")
        try:
            with open(path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        if not data:
            return
        # don't split a line we may still be in the middle of
        cut = data.rfind(b"\n") + 1
        if not cut:
            return
        self.offset += cut
        adj = self.scroller.get_vadjustment()
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40
        buf.insert(buf.get_end_iter(), clean(data[:cut].decode(errors="replace")))
        if at_bottom:
            buf.move_mark(self.end_mark, buf.get_end_iter())
            self.view.scroll_mark_onscreen(self.end_mark)

    def on_cancel(self, button):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.YES_NO,
                                   text="Stop the update?")
        dialog.format_secondary_text(
            "The package being built is stopped; packages already installed stay "
            "installed. Porthole then checks what is left, so you can start the "
            "rest later.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return
        button.set_sensitive(False)
        run_as_root(service("cancel"), lambda ok, out: (
            button.set_sensitive(True), self.refresh()))
