"""
    Porthole update notifier

    Runs in the user's desktop session. Watches the plan written by the
    root update service (/var/lib/porthole/plan.json) and the job queue
    (jobs.json). The tray icon shows while updates are pending, while a
    job is queued or running, and after a job failed; its tooltip shows
    progress. Notifications announce new plans and finished jobs.
    Everything here runs as the user; only submitting a check asks for
    root (sudo -A, or pkexec).

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import os
import subprocess
import sys
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gio, GLib, Gtk, Notify

from porthole.updater.analyze import format_report, summarize
from porthole.updater.commands import icon_path, script_command
from porthole.updater.elevate import run_as_root, service
from porthole.updater.runner import ACTIVE
from porthole.updater.service import (
    STATE_DIR, STATE_FILE, USER_PLAN_FILE, latest_plan, read_json, world_request)
from porthole.updater.viewer import current_job, describe, read_jobs

APP_ID = "org.porthole.UpdateNotifier"
NOTIFIER_STATE = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "porthole", "notifier.json")
SPAWN_LOG = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "porthole", "notifier.log")
# a check that has claimed to be running this long has died
STALE_RUN = 3 * 3600
PENDING = ("ready", "needs-approval", "needs-help")

class TrayIcon:
    """Gtk.StatusIcon (XEmbed trays such as lxpanel) or an AppIndicator"""

    def __init__(self, on_activate, menu):
        self.menu = menu
        self.indicator = None
        self.status_icon = None
        if self._have_sni_watcher():
            try:
                gi.require_version("AyatanaAppIndicator3", "0.1")
                from gi.repository import AyatanaAppIndicator3 as AppIndicator
                self.indicator = AppIndicator.Indicator.new(
                    APP_ID, icon_path() or "software-update-available",
                    AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
                self.indicator.set_menu(menu)
                self._ai = AppIndicator
            except (ValueError, ImportError):
                self.indicator = None
        if self.indicator is None:
            self.status_icon = Gtk.StatusIcon()
            self._set_icon()
            self.status_icon.connect("activate", lambda *a: on_activate())
            self.status_icon.connect("popup-menu", self._popup)
        self.set_visible(False)

    @staticmethod
    def _have_sni_watcher():
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            owner = bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, -1, None)
            return owner.unpack()[0]
        except GLib.Error:
            return False

    def _set_icon(self):
        # Porthole's own icon, like its windows; a theme icon only without it
        path = icon_path()
        if path:
            self.status_icon.set_from_file(path)
            return
        theme = Gtk.IconTheme.get_default()
        for name in ("software-update-available", "system-software-update"):
            if theme.has_icon(name):
                self.status_icon.set_from_icon_name(name)
                return

    def _popup(self, icon, button, activate_time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    def set_visible(self, visible):
        if self.indicator:
            self.indicator.set_status(self._ai.IndicatorStatus.ACTIVE if visible
                                      else self._ai.IndicatorStatus.PASSIVE)
        else:
            self.status_icon.set_visible(visible)

    def set_tooltip(self, text):
        if self.indicator:
            self.indicator.set_title(text)
        else:
            self.status_icon.set_tooltip_text(text)


class ReportWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Porthole: update report")
        self.set_default_size(820, 600)
        if icon_path():
            self.set_icon_from_file(icon_path())
        self.connect("delete-event", lambda *a: self.hide() or True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, border_width=8)
        self.heading = Gtk.Label(xalign=0)
        box.pack_start(self.heading, False, False, 0)
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow()
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)
        self.buttons = Gtk.ButtonBox(layout_style=Gtk.ButtonBoxStyle.END, spacing=6)
        box.pack_start(self.buttons, False, False, 0)
        self.add(box)

    def show_plan(self, plan, state):
        if plan:
            when = plan.get("generated", "?").replace("T", " ")[:16]
            self.heading.set_markup("<b>%s</b>  (checked %s)" % (
                GLib.markup_escape_text(summarize(plan)[0]), when))
            text = format_report(plan)
        else:
            self.heading.set_markup("<b>No update check has run yet</b>")
            text = ""
        if state.get("running"):
            text = "A check is running now...\n\n" + text
        self.view.get_buffer().set_text(text)
        self.show_all()
        self.present()


class Notifier(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.plan = None
        self.state = {}
        self.notification = None
        self.report = None
        self.monitor = None
        self.reload_pending = False
        self.activated = False
        self.seen = read_json(NOTIFIER_STATE, {}) or {}
        self.job_timer = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Notify.init("Porthole")
        self.hold()  # keep running without a window

        menu = Gtk.Menu()
        self.items = {}
        for label, callback in (
            ("Review and update...", self.review),
            ("Show progress", self.show_progress),
            ("Show report", self.show_report),
            ("Check now", self.check_now),
            ("Sync now", self.sync_now),
            ("Job logs...", self.job_logs),
            ("Cancel update", self.cancel_update),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda w, cb=callback: cb())
            menu.append(item)
            self.items[label] = item
        self.check_item = self.items["Check now"]
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda w: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.tray = TrayIcon(self.on_tray_click, menu)

        # the service replaces plan.json with a rename, so watch the directory
        directory = Gio.File.new_for_path(STATE_DIR)
        try:
            self.monitor = directory.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self.monitor.connect("changed", self.on_changed)
        except GLib.Error:
            self.monitor = None
        # and poll now and then in case the directory didn't exist yet
        GLib.timeout_add_seconds(600, self.reload)
        self.reload()

    def do_activate(self):
        # the first activation is our own startup; later ones come from
        # launching it again (e.g. from a menu) and show the report
        if self.activated:
            self.show_report()
        self.activated = True

    # -- plan handling ----------------------------------------------------

    def on_changed(self, monitor, file, other, event):
        if (file.get_basename() in ("plan.json", "state.json", "jobs.json")
                and not self.reload_pending):
            self.reload_pending = True
            GLib.timeout_add(500, self.reload)

    def reload(self):
        self.reload_pending = False
        # the background check's plan, or the user's own (Check now) if newer
        self.plan, _path = latest_plan()
        if self.plan:
            from porthole.updater import news
            self.plan["news_unread"] = news.unread_count()
        self.state = read_json(STATE_FILE, {}) or {}
        running = (self.state.get("running")
                   and time.time() - self.state.get("started", 0) < STALE_RUN)
        jobs = read_jobs()
        job = current_job(jobs)
        active = bool(job) and job["status"] in ACTIVE
        # a failed job keeps the icon up until its progress has been looked at
        failed = (bool(job) and job["status"] == "failed"
                  and self.seen.get("failed_seen") != job["id"])
        self.check_item.set_sensitive(not running and not active)
        self.items["Sync now"].set_sensitive(not running and not active)
        # only while a job runs, or to see why the last one failed: with
        # nothing running it would replay an old log as if it were live
        self.items["Show progress"].set_sensitive(active or failed)
        self.items["Show progress"].set_label("Show what failed" if failed and not active
                                              else "Show progress")
        self.items["Cancel update"].set_visible(active and job["kind"] == "update")
        # while a job runs, refresh often: the tooltip shows its progress
        # (and setting it again revives a tray tooltip that stopped showing)
        if active and self.job_timer is None:
            self.job_timer = GLib.timeout_add_seconds(15, self.job_tick)

        pending = bool(self.plan) and self.plan.get("status") in PENDING
        title, body = summarize(self.plan) if self.plan else ("No update check yet", "")
        if active or failed:
            tooltip = describe(job)
        elif running:
            tooltip = "Checking for updates..."
        else:
            tooltip = "%s\n%s" % (title, body) if body else title
        self.tray.set_tooltip(tooltip)
        self.tray.set_visible(pending or bool(running) or active or failed)
        self.announce_finished(jobs)

        generated = (self.plan or {}).get("generated")
        if pending and generated and generated != self.seen.get("notified"):
            self.notify(title, body)
            self.seen["notified"] = generated
            self.save_seen()
        if self.report and self.report.get_visible():
            self.report.show_plan(self.plan, self.state)
        return True  # keep the poll timer going

    def announce_finished(self, jobs):
        """One notification per job that finished since we last looked"""
        last = self.seen.get("job_announced")
        finished = [j for j in jobs if j["status"] not in ACTIVE]
        if last is None:
            # first run: don't announce history
            if finished:
                self.seen["job_announced"] = finished[-1]["id"]
                self.save_seen()
            return
        ids = [j["id"] for j in finished]
        new = finished[ids.index(last) + 1:] if last in ids else finished[-1:]
        for job in new:
            if job["kind"] == "check" and job["status"] == "done":
                continue  # the new plan has its own notification
            words = {"done": "finished", "failed": "failed", "cancelled": "was cancelled"}
            body = {"done": "Everything in the plan was installed.",
                    "failed": "A step failed; the rest was skipped. Open the progress "
                              "window to see why.",
                    "cancelled": "Packages already built stay installed."}
            self.notify("%s %s" % (job["title"], words.get(job["status"], job["status"])),
                        body.get(job["status"], ""), progress=True)
        if new:
            self.seen["job_announced"] = new[-1]["id"]
            self.save_seen()

    def save_seen(self):
        try:
            os.makedirs(os.path.dirname(NOTIFIER_STATE), exist_ok=True)
            with open(NOTIFIER_STATE, "w") as f:
                json.dump(self.seen, f)
        except OSError:
            pass

    def notify(self, title, body, progress=False):
        self.notification = Notify.Notification.new(
            title, body, icon_path() or "software-update-available")
        if "actions" in (Notify.get_server_caps() or []):
            if progress:
                self.notification.add_action("progress", "Show progress",
                                             lambda *a: self.show_progress())
            else:
                self.notification.add_action("review", "Review and update",
                                             lambda *a: self.review())
                self.notification.add_action("report", "Show report",
                                             lambda *a: self.show_report())
        try:
            self.notification.show()
        except GLib.Error:
            pass

    # -- actions -----------------------------------------------------------

    def on_tray_click(self):
        job = current_job()
        if job and (job["status"] in ACTIVE or (
                job["status"] == "failed" and self.seen.get("failed_seen") != job["id"])):
            self.show_progress()
        else:
            self.show_report()

    def job_tick(self):
        job = current_job()
        if not job or job["status"] not in ACTIVE:
            self.job_timer = None
            self.reload()
            return False
        self.reload()
        return True

    def cancel_update(self):
        dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.YES_NO, text="Stop the update?")
        dialog.set_title("Porthole")
        dialog.format_secondary_text(
            "The package being built is stopped; packages already installed stay "
            "installed. Porthole then checks what is left, so you can start the rest "
            "later.")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            run_as_root(service("cancel"), lambda ok, out: self.reload())

    def show_progress(self):
        # Porthole's terminal: colours, warnings and cautions tabs, summary;
        # what failed is shown in a log viewer
        job = current_job()
        if job and job["status"] == "failed":
            self.spawn(script_command("porthole") + ["--job-log", job["id"]])
            self.seen["failed_seen"] = job["id"]
            self.save_seen()
            self.reload()
        else:
            self.spawn(script_command("porthole") + ["--follow-job"])

    def job_logs(self):
        self.spawn(script_command("porthole") + ["--job-logs"])

    def show_report(self):
        if self.report is None:
            self.report = ReportWindow()
            self.add_window(self.report)
            review = Gtk.Button(label="Review and update...")
            review.connect("clicked", lambda b: self.review())
            close = Gtk.Button(label="Close")
            close.connect("clicked", lambda b: self.report.hide())
            self.report.buttons.add(review)
            self.report.buttons.add(close)
        self.report.show_plan(self.plan, self.state)

    def review(self):
        # Porthole takes over from here
        if self.report:
            self.report.hide()
        # as the user: Porthole only asks for root to change something
        self.spawn(script_command("porthole") + ["--review"])

    def check_now(self):
        """Work out the update as the user: no password, no sync (the
        background check syncs once a day)"""
        from porthole.updater.elevate import plan_as_user
        self.check_item.set_sensitive(False)
        self.tray.set_tooltip("Checking for updates...")
        plan_as_user(world_request(), self.check_done, out=USER_PLAN_FILE)

    def sync_now(self):
        """Sync and check as a runner job, shown in the job window"""
        from porthole.updater.elevate import sync_now
        sync_now(None, lambda job_id: self.show_progress())

    def check_done(self, plan_file, error):
        self.check_item.set_sensitive(True)
        if not plan_file:
            Notify.Notification.new("Couldn't check for updates", error, None).show()
        self.reload()

    def spawn(self, command):
        # keep the output: pkexec and the programs it runs report failures there
        try:
            os.makedirs(os.path.dirname(SPAWN_LOG), exist_ok=True)
            log = open(SPAWN_LOG, "a")
            log.write("\n%s $ %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        " ".join(command)))
            log.flush()
        except OSError:
            log = subprocess.DEVNULL
        try:
            subprocess.Popen(command, start_new_session=True,
                             stdout=log, stderr=subprocess.STDOUT)
        except OSError as e:
            Notify.Notification.new("Porthole", "Could not start %s: %s"
                                    % (command[0], e), None).show()


def main(argv=None):
    return Notifier().run(argv if argv is not None else sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
