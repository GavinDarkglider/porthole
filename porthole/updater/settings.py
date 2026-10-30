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
from porthole.updater.session import TIMEOUTS, unlock_minutes
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
        heading.set_markup("<b>Automatic sync</b>")
        self.attach(heading, 0, row, 2, 1)
        row += 1

        self.attach(Gtk.Label(label="Sync the repositories every", xalign=0), 0, row, 1, 1)
        days = Gtk.Box(spacing=6)
        self.interval = Gtk.SpinButton.new_with_range(1, 90, 1)
        self.interval.set_value(config["interval_days"])
        days.pack_start(self.interval, False, False, 0)
        days.pack_start(Gtk.Label(label="day(s)"), False, False, 0)
        self.attach(days, 1, row, 1, 1)
        row += 1

        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        hint.set_markup("<small>Counted from the last sync, also one you ran yourself. "
                        "Never more than once a day, to be fair to the Gentoo "
                        "mirrors.</small>")
        self.attach(hint, 0, row, 2, 1)
        row += 1

        self.check = Gtk.CheckButton(label="Check for updates after each sync")
        self.check.set_tooltip_text("Work out what can be updated once the sync is done; "
                                    "the tray tells you when there is something")
        self.check.set_active(config["check_updates"])
        self.attach(self.check, 0, row, 2, 1)
        row += 1

        self.battery = Gtk.CheckButton(label="Don't sync while running on battery")
        self.battery.set_active(config["skip_on_battery"])
        self.attach(self.battery, 0, row, 2, 1)
        row += 1

        heading = Gtk.Label(xalign=0, margin_top=8)
        heading.set_markup("<b>Live packages</b>")
        self.attach(heading, 0, row, 2, 1)
        row += 1
        self.live = Gtk.CheckButton(
            label="Rebuild live (-9999) packages that have new upstream commits")
        self.live.set_active(config["live_rebuild"])
        self.attach(self.live, 0, row, 2, 1)
        row += 1
        self.attach(Gtk.Label(label="Never rebuild these", xalign=0), 0, row, 1, 1)
        self.live_exclude = Gtk.Entry(text=" ".join(config["live_exclude"]),
                                      placeholder_text="cat/pkg cat/pkg ...", hexpand=True)
        self.attach(self.live_exclude, 1, row, 1, 1)
        row += 1
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        hint.set_markup("<small>Checked with smart-live-rebuild, which only asks the "
                        "upstream repositories for their latest commit.</small>")
        self.attach(hint, 0, row, 2, 1)
        row += 1

        heading = Gtk.Label(xalign=0, margin_top=8)
        heading.set_markup("<b>Packages nothing needs</b>")
        self.attach(heading, 0, row, 2, 1)
        row += 1
        self.depclean = Gtk.CheckButton(
            label="List packages nothing needs any more, to remove them after an update")
        self.depclean.set_tooltip_text(
            "emerge --depclean, worked out with each check. The update assistant lists "
            "them; only the ones you tick are removed.")
        self.depclean.set_active(config["offer_depclean"])
        self.attach(self.depclean, 0, row, 2, 1)
        row += 1

        heading = Gtk.Label(xalign=0, margin_top=8)
        heading.set_markup("<b>Unlocking</b>")
        self.attach(heading, 0, row, 2, 1)
        row += 1
        self.attach(Gtk.Label(label="Unlock Porthole for", xalign=0), 0, row, 1, 1)
        self.unlock_time = Gtk.ComboBoxText()
        for minutes, label in TIMEOUTS:
            self.unlock_time.append(str(minutes), label)
        self.unlock_time.set_active_id(str(unlock_minutes()))
        self.attach(self.unlock_time, 1, row, 1, 1)
        row += 1
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        hint.set_markup("<small>Unlock (on the toolbar) asks for the password once; "
                        "until this time is up changes need no password. It is "
                        "your own setting, used from the next unlock.</small>")
        self.attach(hint, 0, row, 2, 1)
        row += 1

        status = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        status.set_markup("<small>Last sync: %s.  Last check: %s.\nThe schedule runs "
                          "from cron every hour and only does anything when due. Check "
                          "now in the update assistant never needs a sync. Changing "
                          "the settings above asks for administrator rights.</small>"
                          % (_ago(last_repo_sync()), _ago(state.get("last_check"))))
        self.attach(status, 0, row, 2, 1)

    def values(self):
        return {"interval_days": self.interval.get_value_as_int(),
                "check_updates": self.check.get_active(),
                "skip_on_battery": self.battery.get_active(),
                "live_rebuild": self.live.get_active(),
                "live_exclude": self.live_exclude.get_text().split(),
                "offer_depclean": self.depclean.get_active()}

    def save(self, callback=None):
        """Save if anything changed; callback(ok, message) when done"""
        from porthole import config
        config.Prefs.globals.unlock_minutes = int(self.unlock_time.get_active_id())
        values = self.values()
        if values == self.saved_config:
            if callback:
                callback(True, "")
            return
        settings = ["interval_days=%d" % values["interval_days"],
                    "check_updates=%s" % ("yes" if values["check_updates"] else "no"),
                    "skip_on_battery=%s" % ("yes" if values["skip_on_battery"] else "no"),
                    "live_rebuild=%s" % ("yes" if values["live_rebuild"] else "no"),
                    "live_exclude=%s" % " ".join(values["live_exclude"]),
                    "offer_depclean=%s" % ("yes" if values["offer_depclean"] else "no")]

        def done(ok, output):
            if ok:
                self.saved_config = values
            if callback:
                callback(ok, output)
        run_as_root(service("config", *settings), done)
