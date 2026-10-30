"""
    Porthole: the logs of past runner jobs

    Lists the jobs the root job runner keeps (the last KEEP_JOBS) and
    shows a job's log in Porthole's terminal, as a log viewer: the same
    colouring and warnings, cautions and summary tabs, but nothing runs
    there, so no queue and no Kill. A job still running is followed in
    the job window instead.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from porthole.updater.runner import ACTIVE, JOBS_FILE  # noqa: E402
from porthole.updater.service import read_json  # noqa: E402

STATUS = {"done": "Finished", "failed": "Failed", "cancelled": "Stopped",
          "queued": "Waiting", "running": "Running"}


def jobs():
    """The runner's jobs, newest first"""
    return list(reversed((read_json(JOBS_FILE, {}) or {}).get("jobs", [])))


def _when(job):
    stamp = job.get("finished") or job.get("started") or job.get("submitted") or 0
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else ""


def show_log(job, on_close=None):
    """Show a finished job's log in a log viewer window; a job still
    running is followed in the job window"""
    if job["status"] in ACTIVE:
        from porthole.updater.follow import show_in_terminal
        show_in_terminal(job["id"])
        return None
    return open_file(job.get("log") or "", "%s (%s, %s)" % (
        job["title"], STATUS.get(job["status"], job["status"]).lower(), _when(job)),
        on_close)


def open_file(path, title=None, on_close=None):
    """A log viewer window showing the log file at path"""
    from porthole.terminal.terminal import ProcessManager
    from porthole.utils import utils
    viewer = ProcessManager(utils.environment(), True)
    viewer.on_close = on_close
    if not os.access(path, os.R_OK):
        viewer.show_window()
        viewer.set_statusbar("*** The log %s is gone (the runner keeps the last jobs' logs)"
                             % path)
        return viewer
    viewer.open_ok_func(path)
    if title:
        viewer.window.set_title("Porthole log: " + title)
    return viewer


class JobLogsWindow(Gtk.Window):
    """Past jobs; Show log opens the chosen one"""

    def __init__(self, parent=None, on_close=None):
        super().__init__(title="Porthole: job logs", transient_for=parent)
        from porthole.updater.commands import icon_path
        if icon_path():
            self.set_icon_from_file(icon_path())
        self.set_default_size(760, 420)
        self.on_close = on_close
        self.connect("destroy", self.closed)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
        label = Gtk.Label(xalign=0, wrap=True, label=(
            "The output of the jobs Porthole's job runner did: installs, updates, "
            "syncs and removals. The runner keeps the last ones."))
        box.pack_start(label, False, False, 0)
        # id, when, title, status, by
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, title in ((1, "When"), (3, "Status"), (2, "Job"), (4, "By")):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            column.set_resizable(True)
            column.set_expand(i == 2)
            self.view.append_column(column)
        self.view.connect("row-activated", lambda v, path, c: self.show_selected())
        self.view.get_selection().connect("changed", lambda s: self.update_buttons())
        scroller = Gtk.ScrolledWindow()
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda b: self.destroy())
        other = Gtk.Button(label="Open a saved log...")
        other.connect("clicked", lambda b: self.open_other())
        self.show_button = Gtk.Button(label="Show log")
        self.show_button.connect("clicked", lambda b: self.show_selected())
        buttons.pack_start(close, False, False, 0)
        buttons.pack_start(other, False, False, 0)
        buttons.pack_end(self.show_button, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self.viewers = []
        self.refresh()
        self.show_all()

    def refresh(self):
        self.jobs = {job["id"]: job for job in jobs()}
        self.store.clear()
        for job in self.jobs.values():
            self.store.append([job["id"], _when(job), job.get("title", job["id"]),
                               STATUS.get(job["status"], job["status"]), job.get("by", "")])
        if len(self.store):
            self.view.get_selection().select_path(Gtk.TreePath.new_first())
        self.update_buttons()

    def selected(self):
        model, it = self.view.get_selection().get_selected()
        return self.jobs.get(model[it][0]) if it else None

    def update_buttons(self):
        job = self.selected()
        self.show_button.set_sensitive(job is not None)
        self.show_button.set_label("Show progress" if job and job["status"] in ACTIVE
                                   else "Show log")

    def show_selected(self):
        job = self.selected()
        if job:
            self.keep(show_log(job, self.viewer_closed))

    def open_other(self):
        """A log saved from a terminal window (Save log)"""
        from porthole import config
        dialog = Gtk.FileChooserDialog(title="Porthole: open a saved log", transient_for=self,
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Open", Gtk.ResponseType.OK)
        directory = getattr(config.Prefs, "LOG_FILE_DIR", None)
        if directory and os.path.isdir(directory):
            dialog.set_current_folder(directory)
        logs = Gtk.FileFilter()
        logs.set_name("Logs")
        for pattern in ("*.log", "*.warning", "*.caution", "*.summary"):
            logs.add_pattern(pattern)
        dialog.add_filter(logs)
        everything = Gtk.FileFilter()
        everything.set_name("All files")
        everything.add_pattern("*")
        dialog.add_filter(everything)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        if path:
            self.keep(open_file(path, os.path.basename(path), self.viewer_closed))

    def keep(self, viewer):
        if viewer is not None:
            self.viewers.append(viewer)

    def viewer_closed(self, viewer):
        if viewer in self.viewers:
            self.viewers.remove(viewer)
        self.maybe_done()

    def closed(self, window):
        self.list_closed = True
        self.maybe_done()

    def maybe_done(self):
        """For 'porthole --job-logs': done when the list and every log
        it opened are closed"""
        if getattr(self, "list_closed", False) and not self.viewers and self.on_close:
            self.on_close()
            self.on_close = None


def run_window(on_close, job_id=None):
    """For 'porthole --job-logs [JOB]': the list, or just that job's log"""
    if job_id:
        job = next((j for j in jobs() if j["id"] == job_id), None)
        if job:
            if job["status"] in ACTIVE:
                show_log(job)
                return False
            show_log(job, lambda viewer: on_close())
            return True
    JobLogsWindow(on_close=on_close)
    return True
