"""
    Porthole update notifier

    Runs in the user's desktop session. Watches the plan written by the
    root update service (/var/lib/porthole/plan.json), shows a tray icon
    while updates are pending and a notification for each new plan.
    Opening Porthole or starting a check asks for the admin password
    through pkexec.

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
from porthole.updater.commands import LOCAL, SRC as _SRC, script_command
from porthole.updater.service import PLAN_FILE, STATE_DIR, STATE_FILE, read_json

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

def as_root(command, gui=False):
    """Run through pkexec. Installed programs have a polkit policy that
    keeps the display; from a checkout it must be passed along"""
    if gui and LOCAL:
        env = ["%s=%s" % (k, os.environ[k]) for k in ("DISPLAY", "XAUTHORITY")
               if k in os.environ]
        command = ["env"] + env + command
    return ["pkexec"] + command


def icon_path():
    for base in (os.path.join(_SRC, "porthole"), "/usr/share/porthole"):
        path = os.path.join(base, "pixmaps", "porthole-icon.png")
        if os.path.exists(path):
            return path
    return None


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
                    APP_ID, "software-update-available",
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
        theme = Gtk.IconTheme.get_default()
        for name in ("software-update-available", "system-software-update"):
            if theme.has_icon(name):
                self.status_icon.set_from_icon_name(name)
                return
        path = icon_path()
        if path:
            self.status_icon.set_from_file(path)

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

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Notify.init("Porthole")
        self.hold()  # keep running without a window

        menu = Gtk.Menu()
        for label, callback in (
            ("Review and update...", self.review),
            ("Show report", self.show_report),
            ("Check now", self.check_now),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda w, cb=callback: cb())
            menu.append(item)
            if label == "Check now":
                self.check_item = item
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda w: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.tray = TrayIcon(self.show_report, menu)

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
        if file.get_basename() in ("plan.json", "state.json") and not self.reload_pending:
            self.reload_pending = True
            GLib.timeout_add(500, self.reload)

    def reload(self):
        self.reload_pending = False
        self.plan = read_json(PLAN_FILE)
        self.state = read_json(STATE_FILE, {}) or {}
        running = (self.state.get("running")
                   and time.time() - self.state.get("started", 0) < STALE_RUN)
        self.check_item.set_sensitive(not running)

        pending = bool(self.plan) and self.plan.get("status") in PENDING
        title, body = summarize(self.plan) if self.plan else ("No update check yet", "")
        self.tray.set_tooltip("Checking for updates..." if running else
                              "%s\n%s" % (title, body) if body else title)
        self.tray.set_visible(pending or bool(running))

        generated = (self.plan or {}).get("generated")
        if pending and generated and generated != self.seen.get("notified"):
            self.notify(title, body)
            self.seen["notified"] = generated
            self.save_seen()
        if self.report and self.report.get_visible():
            self.report.show_plan(self.plan, self.state)
        return True  # keep the poll timer going

    def save_seen(self):
        try:
            os.makedirs(os.path.dirname(NOTIFIER_STATE), exist_ok=True)
            with open(NOTIFIER_STATE, "w") as f:
                json.dump(self.seen, f)
        except OSError:
            pass

    def notify(self, title, body):
        self.notification = Notify.Notification.new(
            title, body, icon_path() or "software-update-available")
        if "actions" in (Notify.get_server_caps() or []):
            self.notification.add_action("review", "Review and update",
                                         lambda *a: self.review())
            self.notification.add_action("report", "Show report",
                                         lambda *a: self.show_report())
        try:
            self.notification.show()
        except GLib.Error:
            pass

    # -- actions -----------------------------------------------------------

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
        self.spawn(as_root(script_command("porthole") + ["--review"], gui=True))

    def check_now(self):
        self.check_item.set_sensitive(False)
        self.tray.set_tooltip("Checking for updates...")
        self.spawn(as_root(script_command("porthole-update-service")
                           + ["run", "--force", "--quiet"]))

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
