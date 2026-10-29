"""
    Porthole update assistant: settings

    The Updates page of Porthole's settings window, for
    /etc/porthole/updater.conf. Anyone can look; saving goes through
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


class UpdaterSettingsPage(Gtk.Grid):
    def __init__(self):
        super().__init__(row_spacing=10, column_spacing=12, border_width=12)
        self.saved_config = load_config()
        config = self.saved_config
        state = read_json(STATE_FILE, {}) or {}
        row = 0

        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Automatic update checks</b>")
        self.attach(heading, 0, row, 2, 1)
        row += 1

        self.attach(Gtk.Label(label="Check for updates every", xalign=0), 0, row, 1, 1)
        days = Gtk.Box(spacing=6)
        self.interval = Gtk.SpinButton.new_with_range(1, 90, 1)
        self.interval.set_value(config["interval_days"])
        days.pack_start(self.interval, False, False, 0)
        days.pack_start(Gtk.Label(label="day(s)"), False, False, 0)
        self.attach(days, 1, row, 1, 1)
        row += 1

        self.sync = Gtk.CheckButton(label="Sync the repositories before checking")
        self.sync.set_active(config["sync"])
        self.attach(self.sync, 0, row, 2, 1)
        row += 1
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        hint.set_markup("<small>Syncs happen at most once a day, even when you check "
                        "by hand, to be fair to the Gentoo mirrors.</small>")
        self.attach(hint, 0, row, 2, 1)
        row += 1

        self.battery = Gtk.CheckButton(label="Don't check while running on battery")
        self.battery.set_active(config["skip_on_battery"])
        self.attach(self.battery, 0, row, 2, 1)
        row += 1

        status = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        status.set_markup("<small>Last check: %s.  Last sync: %s.\nChecks run from cron "
                          "every hour and only do anything when due. Changing these "
                          "asks for administrator rights.</small>"
                          % (_ago(state.get("last_check")), _ago(last_repo_sync())))
        self.attach(status, 0, row, 2, 1)

    def values(self):
        return {"interval_days": self.interval.get_value_as_int(),
                "sync": self.sync.get_active(),
                "skip_on_battery": self.battery.get_active()}

    def save(self, callback=None):
        """Save if anything changed; callback(ok, message) when done"""
        values = self.values()
        if values == self.saved_config:
            if callback:
                callback(True, "")
            return
        settings = ["interval_days=%d" % values["interval_days"],
                    "sync=%s" % ("yes" if values["sync"] else "no"),
                    "skip_on_battery=%s" % ("yes" if values["skip_on_battery"] else "no")]

        def done(ok, output):
            if ok:
                self.saved_config = values
            if callback:
                callback(ok, output)
        run_as_root(service("config", *settings), done)
