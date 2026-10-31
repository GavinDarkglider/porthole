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
from gi.repository import GLib, Gtk

from porthole.updater.elevate import run_as_root, service
from porthole.updater.window import format_window, parse_window
from porthole.updater.service import setting_text, STATE_FILE, last_repo_sync, load_config, read_json


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
        heading.set_markup("<b>How updates are run</b>")
        self.attach(heading, 0, row, 2, 1)
        row += 1
        if config["automation"] is None:
            note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
            note.set_markup("<small><i>Not chosen yet: Porthole asks when it starts. "
                            "Choose here and save to answer it now.</i></small>")
            self.attach(note, 0, row, 2, 1)
            row += 1
        self.automation = AutomationBox(config)
        self.attach(self.automation, 0, row, 2, 1)
        row += 1

        heading = Gtk.Label(xalign=0, margin_top=8)
        heading.set_markup("<b>Reminders</b> <small>(just for you, no password "
                           "needed)</small>")
        self.attach(heading, 0, row, 2, 1)
        row += 1
        from porthole.updater.notifier import reminders_enabled
        self.reminders = Gtk.CheckButton(
            label="Remind me when updates have been waiting a few days")
        self.reminders.set_tooltip_text(
            "The tray reminds you after 3 days and every 3 days after that, more "
            "firmly the longer updates wait. Not while an update is queued or "
            "building.")
        self.reminders.set_active(reminders_enabled())
        self.attach(self.reminders, 0, row, 2, 1)
        row += 1

        status = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        status.set_markup("<small>Last sync: %s.  Last check: %s.\nThe schedule runs "
                          "from cron every hour and only does anything when due. Check "
                          "now in the update assistant never needs a sync. Apart from "
                          "reminders, changing these settings asks for administrator "
                          "rights.</small>"
                          % (_ago(last_repo_sync()), _ago(state.get("last_check"))))
        self.attach(status, 0, row, 2, 1)

    def values(self):
        return {"interval_days": self.interval.get_value_as_int(),
                "check_updates": self.check.get_active(),
                "skip_on_battery": self.battery.get_active(),
                "live_rebuild": self.live.get_active(),
                "live_exclude": self.live_exclude.get_text().split(),
                "offer_depclean": self.depclean.get_active(),
                **self.automation.values()}

    def save(self, callback=None):
        """Save if anything changed; callback(ok, message) when done"""
        from porthole.updater.notifier import set_reminders
        set_reminders(self.reminders.get_active())  # the user's own: no root
        try:
            values = self.values()
        except ValueError as e:
            if callback:
                callback(False, str(e))
            return
        saved = dict(self.saved_config)
        if saved["automation"] is None and values["automation"] == "notify":
            # not chosen yet, and left as shown: still the first-start question's
            values["automation"] = None
            if values == saved:
                values["build_window"] = saved["build_window"]
        if values == saved:
            if callback:
                callback(True, "")
            return
        settings = ["interval_days=%d" % values["interval_days"],
                    "check_updates=%s" % ("yes" if values["check_updates"] else "no"),
                    "skip_on_battery=%s" % ("yes" if values["skip_on_battery"] else "no"),
                    "live_rebuild=%s" % ("yes" if values["live_rebuild"] else "no"),
                    "live_exclude=%s" % " ".join(values["live_exclude"]),
                    "offer_depclean=%s" % ("yes" if values["offer_depclean"] else "no")]
        settings += automation_settings(values)

        def done(ok, output):
            if ok:
                self.saved_config = values
            if callback:
                callback(ok, output)
        run_as_root(service("config", *settings), done)


# the automation levels offered, in order: (value, title, explanation)
LEVELS = (
    ("notify", "Tell me, I start updates myself",
     "Porthole checks for updates and the tray tells you. Nothing is built until "
     "you start it."),
    ("approve", "I approve updates, they are built in the build window",
     "You review and approve each update; it is built at a time nobody is "
     "working. You can still start one straight away."),
    ("safe", "Updates with nothing to decide are built in the build window by themselves",
     "If an update needs no decision from you (no settings to change), it is "
     "queued for the build window without asking. Anything needing a decision "
     "waits for you."),
    ("auto", "Fully automatic: Porthole decides, and tells you what it did in News",
     "Porthole makes the safe decisions itself (only changes it has checked; never "
     "licences, unmasking, compiler switches or removals) and builds in the build "
     "window. Each update gets one news item: what it did, why, and how to change it."),
)
WINDOW_KEYS = ("build_window", "window_needs_ac", "idle_minutes", "pause_when_active",
               "low_priority", "auto_testing")


def _testing_keyword():
    """~amd64, or whatever this machine's testing keyword is"""
    try:
        import portage
        return "~" + portage.settings["ARCH"]
    except Exception:
        return "~arch"


def automation_settings(values):
    """KEY=VALUE arguments for `service config` from AutomationBox values"""
    keys = (("automation",) if values["automation"] else ()) + WINDOW_KEYS
    return ["%s=%s" % (key, setting_text(key, values[key])) for key in keys]


class AutomationBox(Gtk.Grid):
    """How updates are run: the automation level and the build window.
    The same in Settings and in the question on Porthole's first start."""

    def __init__(self, config):
        super().__init__(row_spacing=8, column_spacing=12)
        row = 0
        self.levels = {}
        group = None
        for value, title, explanation in LEVELS:
            button = Gtk.RadioButton.new_with_label_from_widget(group, title)
            group = group or button
            self.levels[value] = button
            self.attach(button, 0, row, 2, 1)
            row += 1
            hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=60, margin_start=26)
            hint.set_markup("<small>%s</small>" % GLib.markup_escape_text(explanation))
            self.attach(hint, 0, row, 2, 1)
            row += 1
            if value == "auto":
                self.testing = Gtk.CheckButton(
                    label="Allow testing (%s) versions when an update needs one"
                    % _testing_keyword(), margin_start=26)
                self.testing.set_tooltip_text(
                    "Sometimes an update only works with a newer version that Gentoo "
                    "still marks as testing. Ticked, the automatic update uses it (and "
                    "says so in News); unticked, it holds back what needs it and leaves "
                    "it for you.")
                self.testing.set_active(config["auto_testing"])
                self.attach(self.testing, 0, row, 2, 1)
                row += 1
        self.levels.get(config["automation"] or "notify").set_active(True)

        self.window_label = Gtk.Label(label="Build window", xalign=0, margin_top=6)
        self.attach(self.window_label, 0, row, 1, 1)
        times = Gtk.Box(spacing=6, margin_top=6)
        text = format_window(config["build_window"] or (60, 360)).split("-")
        self.window_from = Gtk.Entry(text=text[0], width_chars=5, max_length=5)
        self.window_to = Gtk.Entry(text=text[1], width_chars=5, max_length=5)
        times.pack_start(self.window_from, False, False, 0)
        times.pack_start(Gtk.Label(label="to"), False, False, 0)
        times.pack_start(self.window_to, False, False, 0)
        self.attach(times, 1, row, 1, 1)
        row += 1
        self.window_ac = Gtk.CheckButton(label="Only on mains power")
        self.window_ac.set_active(config["window_needs_ac"])
        self.attach(self.window_ac, 0, row, 2, 1)
        row += 1
        self.idle_label = Gtk.Label(label="Wait until nobody has used the computer for",
                                    xalign=0)
        self.attach(self.idle_label, 0, row, 1, 1)
        idle = Gtk.Box(spacing=6)
        self.idle_minutes = Gtk.SpinButton.new_with_range(0, 240, 5)
        self.idle_minutes.set_value(config["idle_minutes"])
        idle.pack_start(self.idle_minutes, False, False, 0)
        idle.pack_start(Gtk.Label(label="minutes"), False, False, 0)
        self.attach(idle, 1, row, 1, 1)
        row += 1
        self.pause = Gtk.CheckButton(label="Pause the build while someone uses the computer")
        self.pause.set_active(config["pause_when_active"])
        self.attach(self.pause, 0, row, 2, 1)
        row += 1
        self.low_priority = Gtk.CheckButton(
            label="Build at low priority (also updates started straight away)")
        self.low_priority.set_tooltip_text(
            "Builds give way to everything else you run: slower to finish, but the "
            "computer stays responsive")
        self.low_priority.set_active(config["low_priority"])
        self.attach(self.low_priority, 0, row, 2, 1)

        for button in self.levels.values():
            button.connect("toggled", self.sensitive)
        self.sensitive()

    def level(self):
        return next(v for v, b in self.levels.items() if b.get_active())

    def sensitive(self, *args):
        on = self.level() != "notify"
        for widget in (self.window_label, self.window_from, self.window_to, self.window_ac,
                       self.idle_label, self.idle_minutes, self.pause):
            widget.set_sensitive(on)
        self.testing.set_sensitive(self.level() == "auto")

    def values(self):
        """The settings as chosen; ValueError if the window isn't one"""
        level = self.level()
        window = None
        if level != "notify":
            window = parse_window("%s-%s" % (self.window_from.get_text().strip(),
                                             self.window_to.get_text().strip()))
        return {"automation": level, "build_window": window,
                "window_needs_ac": self.window_ac.get_active(),
                "idle_minutes": self.idle_minutes.get_value_as_int(),
                "pause_when_active": self.pause.get_active(),
                "low_priority": self.low_priority.get_active(),
                "auto_testing": self.testing.get_active()}


def ask_automation(parent=None):
    """Porthole's first-start question: how updates should be run. Asked
    until answered (it is system-wide, so saving it needs root)."""
    from porthole.updater.elevate import run_as_root, service
    config = load_config()
    if config["automation"] is not None:
        return False
    dialog = Gtk.Dialog(title="Porthole: how should updates work?", transient_for=parent,
                        modal=True)
    dialog.add_buttons("Not now", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    area = dialog.get_content_area()
    area.set_border_width(12)
    area.set_spacing(10)
    intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
    intro.set_markup(
        "<b>How should Porthole keep this computer up to date?</b>\n\n"
        "Porthole checks for updates on a schedule and works out any problems "
        "before anything is built. Choose how much it may do by itself. Building "
        "can take hours, so updates can wait for a time nobody is working.\n\n"
        "<small>This is for the whole computer, so saving asks for the administrator "
        "password. You can change it later in Settings, Updates.</small>")
    area.pack_start(intro, False, False, 0)
    box = AutomationBox(config)
    area.pack_start(box, False, False, 0)
    dialog.show_all()
    state = {"busy": False}

    def response(widget, answer):
        if state["busy"]:
            return
        if answer != Gtk.ResponseType.OK:
            dialog.destroy()  # asked again next time Porthole starts
            return
        try:
            values = box.values()
        except ValueError as e:
            _error(dialog, "That isn't a build window", str(e))
            return
        state["busy"] = True
        dialog.set_sensitive(False)

        def saved(ok, output):
            state["busy"] = False
            dialog.set_sensitive(True)
            if ok:
                dialog.destroy()
            else:
                _error(dialog, "The choice wasn't saved", output or "Cancelled.")
        run_as_root(service("config", *automation_settings(values)), saved)
    dialog.connect("response", response)
    return False


def _error(parent, title, text):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True,
                               message_type=Gtk.MessageType.ERROR,
                               buttons=Gtk.ButtonsType.CLOSE, text=title)
    dialog.format_secondary_text(text)
    dialog.run()
    dialog.destroy()


# -- System: settings of the system itself (root's) ---------------------------------

LICENCE_CHOICES = (
    ("free", "Free software only",
     "Software anyone may use, study, change and share (Gentoo's @FREE)."),
    ("redistributable", "Free software, and software that may be shared",
     "Adds firmware and drivers that aren't free but may be passed on "
     "(@BINARY-REDISTRIBUTABLE). What most people want."),
    ("eula", "Also software with an agreement you accept by using it",
     "Adds software whose licence is an agreement (@EULA): read what you install."),
    ("all", "Every licence",
     "Nothing is held back for its licence."),
    ("system", "As make.conf and the profile say",
     "Porthole doesn't set a policy; ACCEPT_LICENSE decides."),
)


class SystemSettingsPage(Gtk.Grid):
    """Settings of the system itself (now: which licences are fine).
    Saving asks for administrator rights."""

    def __init__(self, target=None):
        super().__init__(row_spacing=8, column_spacing=12, border_width=12)
        from porthole.updater.runner import read_licence_policy
        self.target = target
        self.saved = read_licence_policy(target)
        row = 0
        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Licences</b>")
        self.attach(heading, 0, row, 1, 1)
        row += 1
        intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
        intro.set_markup(
            "<small>Which licences are fine on this computer. Packages under other "
            "licences are held back; Updates asks before accepting one, and fully "
            "automatic updates never do. You can still accept a licence for one "
            "package when Porthole asks.</small>")
        self.attach(intro, 0, row, 1, 1)
        row += 1
        self.choices = {}
        group = None
        for value, title, explanation in LICENCE_CHOICES:
            button = Gtk.RadioButton.new_with_label_from_widget(group, title)
            group = group or button
            self.choices[value] = button
            self.attach(button, 0, row, 1, 1)
            row += 1
            hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=70, margin_start=26)
            if value == "system":
                explanation += " Now: %s" % _effective_licences()
            hint.set_markup("<small>%s</small>" % GLib.markup_escape_text(explanation))
            self.attach(hint, 0, row, 1, 1)
            row += 1
        if self.saved == "custom":
            note = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
            note.set_markup("<small><i>The policy file was edited by hand; choosing "
                            "here replaces it.</i></small>")
            self.attach(note, 0, row, 1, 1)
            row += 1
        self.choices.get(self.saved, self.choices["system"]).set_active(True)
        foot = Gtk.Label(xalign=0, wrap=True, max_width_chars=70, margin_top=8)
        foot.set_markup("<small>Kept in /etc/portage/package.license/00-porthole-licences. "
                        "Changing it asks for administrator rights; check for updates "
                        "afterwards to see what it changes.</small>")
        self.attach(foot, 0, row, 1, 1)

    def value(self):
        return next(v for v, b in self.choices.items() if b.get_active())

    def save(self, callback=None):
        """Save if changed; callback(ok, message)"""
        value = self.value()
        if value == self.saved:
            if callback:
                callback(True, "")
            return
        args = ["licences", value] + (["--target", self.target] if self.target else [])

        def done(ok, output):
            if ok:
                self.saved = value
            if callback:
                callback(ok, output)
        run_as_root(service(*args), done)


def _effective_licences():
    """ACCEPT_LICENSE as portage sees it now (make.conf and profile)"""
    try:
        import portage
        return portage.settings.get("ACCEPT_LICENSE", "").strip() or "(not set)"
    except Exception:
        return "(unknown)"
