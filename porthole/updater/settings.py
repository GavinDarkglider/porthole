"""
    Porthole update assistant: settings

    Edits /etc/porthole/updater.conf. Anyone can look; saving goes through
    `porthole-update-service config` as root, which checks the values.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from porthole.updater.elevate import run_as_root, service
from porthole.updater.service import STATE_FILE, last_repo_sync, load_config, read_json


def _ago(when):
    if not when:
        return "never"
    hours = (time.time() - when) / 3600
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return "%d hour%s ago" % (hours, "s" if int(hours) != 1 else "")
    return "%d days ago" % (hours / 24)


class UpdaterSettings(Gtk.Dialog):
    def __init__(self, parent=None):
        super().__init__(title="Porthole: update settings", transient_for=parent,
                         modal=True)
        self.set_default_size(460, -1)
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        config = load_config()
        state = read_json(STATE_FILE, {}) or {}

        grid = Gtk.Grid(row_spacing=10, column_spacing=12, border_width=12)
        row = 0

        grid.attach(Gtk.Label(label="Check for updates every", xalign=0), 0, row, 1, 1)
        days = Gtk.Box(spacing=6)
        self.interval = Gtk.SpinButton.new_with_range(1, 90, 1)
        self.interval.set_value(config["interval_days"])
        days.pack_start(self.interval, False, False, 0)
        days.pack_start(Gtk.Label(label="day(s)"), False, False, 0)
        grid.attach(days, 1, row, 1, 1)
        row += 1

        self.sync = Gtk.CheckButton(label="Sync the repositories before checking")
        self.sync.set_active(config["sync"])
        grid.attach(self.sync, 0, row, 2, 1)
        row += 1
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=56)
        hint.set_markup("<small>Syncs happen at most once a day, even when you check "
                        "by hand, to be fair to the Gentoo mirrors.</small>")
        grid.attach(hint, 0, row, 2, 1)
        row += 1

        self.battery = Gtk.CheckButton(label="Don't check while running on battery")
        self.battery.set_active(config["skip_on_battery"])
        grid.attach(self.battery, 0, row, 2, 1)
        row += 1

        status = Gtk.Label(xalign=0, wrap=True, max_width_chars=56)
        status.set_markup("<small>Last check: %s.  Last sync: %s.\nChecks run from cron "
                          "every hour and only do anything when due.</small>"
                          % (_ago(state.get("last_check")), _ago(last_repo_sync())))
        grid.attach(status, 0, row, 2, 1)

        self.error = Gtk.Label(xalign=0, wrap=True, max_width_chars=56)
        grid.attach(self.error, 0, row + 1, 2, 1)
        self.get_content_area().add(grid)
        self.connect("response", self.on_response)
        self.show_all()
        self.error.hide()

    def on_response(self, dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        self.set_sensitive(False)
        settings = [
            "interval_days=%d" % self.interval.get_value_as_int(),
            "sync=%s" % ("yes" if self.sync.get_active() else "no"),
            "skip_on_battery=%s" % ("yes" if self.battery.get_active() else "no"),
        ]
        # the dialog stays open until saving is done
        run_as_root(service("config", *settings), self.saved)

    def saved(self, ok, output):
        if ok:
            self.destroy()
            return
        self.set_sensitive(True)
        self.error.set_markup("<b>Couldn't save:</b> %s" % (output or "cancelled"))
        self.error.show()
