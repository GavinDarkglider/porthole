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
from gi.repository import Gdk, GLib, Gtk

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


class LicencesPage(Gtk.Grid):
    """Which licences are fine (System > Licences). Saving asks for
    administrator rights."""

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


class SystemSettingsPage(Gtk.Notebook):
    """System: the Portage settings of this computer, as written in its
    make.conf and profile, with facts about the machine. make.conf is
    never written: USE changes go in Porthole's own global USE file
    (package.use/00-porthole-global), previewed first; licences can be set.
    on_applied() runs after USE changes were saved (to work out the
    rebuild in Updates)."""

    def __init__(self, target=None, on_applied=None):
        super().__init__(border_width=6)
        self.target = target
        self.on_applied = on_applied
        self.info = None
        self.pending = {"use": {}, "expand": {}}  # value None: back to make.conf's
        self.preview_labels = []
        self.use_view = "categories"  # or "make.conf": as the file groups them
        self.show_profile = False
        self.licences = LicencesPage(target)
        self.tabs = {}
        for name, title in (("overview", "Overview"), ("use", "USE flags"),
                            ("hardware", "Hardware"), ("build", "Build options")):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                          border_width=12, valign=Gtk.Align.START)
            box.pack_start(_small("Reading the settings..."), False, False, 0)
            self.tabs[name] = box
            self._append(box, title)
        self._append(self.licences, "Licences")
        import threading
        threading.Thread(target=self._read, daemon=True).start()

    def _append(self, widget, title):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(widget)
        self.append_page(scroller, Gtk.Label(label=title))

    def save(self, callback=None):
        self.licences.save(callback)

    def _read(self):
        """The slow part (all packages' flags, gcc, cpuid2cpuflags), not
        in the main loop"""
        from porthole.updater import system
        info = {}
        try:
            info["conf"] = system.read_make_conf()
            info["profile"] = system.profile()[0]
            info["profile_use"] = system.profile_use()
            info["machine"] = system.machine()
            info["descriptions"], info["local"] = system.flag_descriptions()
            info["global"] = system.global_flags()
            try:
                import portage
                info["arch"] = portage.settings["ARCH"]
            except Exception:
                info["arch"] = None
            info["iuse"] = system.all_iuse()
            info["findings"] = system.findings(info["conf"], info["machine"], info["iuse"])
            info["expand"] = {name: system.expand_values(name) for name in system.EXPAND}
            info["overrides"] = system.read_global_use()
        except Exception as e:
            info["error"] = "%s: %s" % (type(e).__name__, e)
        GLib.idle_add(self._show, info)

    def _show(self, info):
        for box in self.tabs.values():
            for child in box.get_children():
                child.destroy()
        if "error" in info:
            self.tabs["overview"].pack_start(
                _small("Couldn't read the settings: " + info["error"]), False, False, 0)
            self.show_all()
            return False
        self.info = info
        self._overview(info)
        self._refresh()
        self._build(info)
        self.show_all()
        return False

    # -- changes (Porthole's global USE file) --

    def _effective(self):
        """Porthole's global USE as it would be with the pending changes"""
        model = {"use": dict(self.info["overrides"]["use"]),
                 "expand": dict(self.info["overrides"]["expand"])}
        for kind in ("use", "expand"):
            for key, value in self.pending[kind].items():
                if value is None:
                    model[kind].pop(key, None)
                else:
                    model[kind][key] = value
        return model

    def _base_state(self, flag):
        """Whether make.conf (or else the profile) turns a flag on"""
        state = None
        for group in self.info["conf"]["use_groups"]:
            for word in group["flags"]:
                if word.lstrip("-+") == flag:
                    state = not word.startswith("-")
        if state is None:
            profile = self.info["profile_use"]
            state = flag in profile and "-" + flag not in profile
        return state

    def _flag_state(self, flag):
        """Whether a flag is on: Porthole's file (with the pending
        changes) over make.conf over the profile"""
        porthole = self._effective()["use"]
        return porthole[flag] if flag in porthole else self._base_state(flag)

    def _toggle(self, flag):
        """Switch a flag for every package. Switching it back to what
        make.conf says takes Porthole's change away instead."""
        want = not self._flag_state(flag)
        written = self.info["overrides"]["use"].get(flag)  # in the file now
        if want == self._base_state(flag):
            if written is None:
                self.pending["use"].pop(flag, None)
            else:
                self.pending["use"][flag] = None
        elif written == want:
            self.pending["use"].pop(flag, None)
        else:
            self.pending["use"][flag] = want
        self._refresh()

    def _set_expand(self, name, values):
        self.pending["expand"][name] = values
        self._refresh()

    def _changes_text(self):
        parts = []
        for flag, value in sorted(self.pending["use"].items()):
            parts.append(("%s back to make.conf's" % flag) if value is None
                         else ("+" if value else "-") + flag)
        for name, values in sorted(self.pending["expand"].items()):
            parts.append("%s back to make.conf's" % name if values is None
                         else "%s: %s" % (name, " ".join(values)))
        return ", ".join(parts)

    def _changes_bar(self):
        """Pending changes, with Preview / Apply / Discard"""
        if not any(self.pending.values()):
            return None
        bar = Gtk.InfoBar(message_type=Gtk.MessageType.QUESTION)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
        label.set_markup("<b>Not applied yet:</b> %s\n<small>These go in Porthole's own "
                         "file, package.use/00-porthole-global, never in make.conf. Preview "
                         "shows what they would rebuild.</small>"
                         % GLib.markup_escape_text(self._changes_text()))
        box.pack_start(label, False, False, 0)
        preview = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
        self.preview_labels.append(preview)
        box.pack_start(preview, False, False, 0)
        bar.get_content_area().add(box)
        bar.add_button("Discard", 1)
        bar.add_button("Preview rebuild", 2)
        bar.add_button("Apply", 3)
        bar.connect("response", self._on_changes)
        self.changes_bar = bar
        return bar

    def _on_changes(self, bar, response):
        if response == 1:
            self.pending = {"use": {}, "expand": {}}
            self._refresh()
        elif response == 2:
            self._preview(bar)
        elif response == 3:
            self._apply(bar)

    def _preview(self, bar):
        import threading
        from porthole.updater import system
        model = self._effective()
        bar.set_sensitive(False)
        for preview in self.preview_labels:
            preview.set_markup("<small>Working out what this rebuilds, on a copy of the "
                               "configuration (a minute or two)...</small>")

        def work():
            try:
                plan = system.preview_global_use(model)
                builds = [m["cpv"] for m in plan.get("merge_list") or []]
                if plan.get("status") in ("ready", "needs-approval", "up-to-date"):
                    text = ("Nothing needs rebuilding." if not builds else
                            "Would rebuild %d package%s: %s" % (
                                len(builds), "" if len(builds) == 1 else "s",
                                ", ".join(builds)))
                    if plan.get("status") == "needs-approval":
                        text += " (Updates will also ask about some settings changes.)"
                else:
                    from porthole.updater.analyze import summarize
                    title, body = summarize(plan)
                    text = "Portage couldn't work it out as it stands: %s %s" % (title, body)
            except Exception as e:
                text = "Couldn't preview it: %s" % e
            GLib.idle_add(done, text)

        def done(text):
            bar.set_sensitive(True)
            for preview in self.preview_labels:
                preview.set_markup("<small>%s</small>" % GLib.markup_escape_text(text))
            return False
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, bar):
        import json
        model = self._effective()
        args = ["global-use", json.dumps(model)] + (
            ["--target", self.target] if self.target else [])
        bar.set_sensitive(False)

        def done(ok, output):
            bar.set_sensitive(True)
            if not ok:
                _error(self.get_toplevel(), "The USE changes weren't saved",
                       output or "Cancelled.")
                return
            self.info["overrides"] = model
            self.pending = {"use": {}, "expand": {}}
            self._refresh()
            if self.on_applied:
                self.on_applied()
        run_as_root(service(*args), done)

    def _profile_changed(self):
        """Read everything again (the profile changes the defaults), and
        work out the rebuild in Updates"""
        import threading
        threading.Thread(target=self._read, daemon=True).start()
        if self.on_applied:
            self.on_applied()

    def _refresh(self):
        """Redraw the tabs that show changes"""
        for name in ("use", "hardware"):
            for child in self.tabs[name].get_children():
                child.destroy()
        self.preview_labels = []
        self._use(self.info)
        self._hardware(self.info)
        self.tabs["use"].show_all()
        self.tabs["hardware"].show_all()

    # -- the tabs --

    def _overview(self, info):
        box, m = self.tabs["overview"], info["machine"]
        box.pack_start(_heading("This computer"), False, False, 0)
        facts = [("Profile", info["profile"] or "?"),
                 ("CPU", "%s (%d threads)" % (m["cpu"] or "?", m["threads"])),
                 ("Memory", "%s GB" % (m["memory_gb"] or "?"))]
        box.pack_start(_facts(facts), False, False, 0)
        change = Gtk.Button(label="Change profile...", halign=Gtk.Align.START, margin_start=6)
        change.connect("clicked", lambda b: ProfileDialog(self.get_toplevel(), info["profile"],
                                                          self._profile_changed))
        box.pack_start(change, False, False, 0)
        box.pack_start(_heading("Worth a look"), False, False, 0)
        if not info["findings"]:
            box.pack_start(_small("Nothing stands out."), False, False, 0)
        for title, detail in info["findings"]:
            item = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=6)
            label = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
            label.set_markup("\u2022 <b>%s</b>" % GLib.markup_escape_text(title))
            item.pack_start(label, False, False, 0)
            item.pack_start(_small(detail, margin_start=14), False, False, 0)
            box.pack_start(item, False, False, 0)
        box.pack_start(_small(
            "These are notes, not changes: Porthole doesn't touch make.conf here.",
            margin_top=6), False, False, 0)

    def _use(self, info):
        box = self.tabs["use"]
        conf, described, iuse = info["conf"], info["descriptions"], info["iuse"]
        bar = self._changes_bar()
        if bar:
            box.pack_start(bar, False, False, 0)
        box.pack_start(_small(
            "Hover a flag for what it does and where it is set; click one to switch it "
            "for every package (in Porthole's own file, not make.conf). Dimmed: off. "
            "Struck through: no package has the flag, so it does nothing."), False, False, 0)
        porthole = self._effective()["use"]
        changed = set(porthole) | set(self.pending["use"])
        mine = {f.lstrip("-+") for g in conf["use_groups"] for f in g["flags"]}
        profile = {f.lstrip("-") for f in info["profile_use"]
                   if "_" not in f.lstrip("-") and f.lstrip("-") != info.get("arch")}

        def source(flag):
            if flag in self.pending["use"]:
                return "Porthole's change, not applied yet"
            if flag in porthole:
                return "Set in Porthole's global USE file"
            if flag in mine:
                return "Set in make.conf"
            return "From the profile (%s)" % (info["profile"] or "?")

        def flag_view(flags):
            return _flags(flags, described, iuse, on_click=self._toggle,
                          state=self._flag_state, changed=changed,
                          pending=set(self.pending["use"]), source=source,
                          packages={f: p for f, p in info["local"].items()
                                    if f not in info["global"]})

        # how to show them
        choices = Gtk.Box(spacing=12)
        by_category = Gtk.RadioButton.new_with_label(None, "By what they do")
        as_written = Gtk.RadioButton.new_with_label_from_widget(by_category,
                                                                "As make.conf groups them")
        (by_category if self.use_view == "categories" else as_written).set_active(True)
        include = Gtk.CheckButton(label="Include the profile's defaults (%d)"
                                  % len(profile - mine))
        include.set_active(self.show_profile)
        include.set_sensitive(self.use_view == "categories")
        for widget in (by_category, as_written, include):
            choices.pack_start(widget, False, False, 0)

        def switch_view(*args):
            self.use_view = "categories" if by_category.get_active() else "make.conf"
            self.show_profile = include.get_active()
            self._refresh()
        # each radio button says when it becomes the chosen one
        for button in (by_category, as_written):
            button.connect("toggled", lambda b: b.get_active() and switch_view())
        include.connect("toggled", switch_view)
        box.pack_start(choices, False, False, 0)

        if self.use_view == "categories":
            from porthole.updater.system import categorise, category_order
            shown = mine | changed | (profile if self.show_profile else set())
            groups = {}
            for flag in shown:
                groups.setdefault(categorise(flag, described, info["local"]), []).append(flag)
            for title in category_order():
                if groups.get(title):
                    box.pack_start(_heading(title), False, False, 0)
                    box.pack_start(flag_view(sorted(groups[title])), False, False, 0)
        else:
            if porthole or self.pending["use"]:
                box.pack_start(_heading("Porthole's changes (package.use/00-porthole-global)"),
                               False, False, 0)
                box.pack_start(flag_view(sorted(changed)), False, False, 0)
            for group in conf["use_groups"]:
                box.pack_start(_heading(group["title"]), False, False, 0)
                for note in group["notes"]:
                    box.pack_start(_small(note), False, False, 0)
                box.pack_start(flag_view(group["flags"]), False, False, 0)
            expander = Gtk.Expander(label="From the profile (%s): %d more"
                                    % (info["profile"] or "?", len(profile - mine)))
            expander.add(flag_view(sorted(profile - mine)))
            box.pack_start(expander, False, False, 0)
        # a flag that isn't listed anywhere
        add = Gtk.Box(spacing=6, margin_top=8)
        add.pack_start(Gtk.Label(label="Another flag:"), False, False, 0)
        entry = Gtk.Entry(placeholder_text="flag name", width_chars=20)
        add.pack_start(entry, False, False, 0)
        for label, on in (("Turn on", True), ("Turn off", False)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda b, on=on: self._add_flag(entry, on))
            add.pack_start(button, False, False, 0)
        box.pack_start(add, False, False, 0)

    def _add_flag(self, entry, on):
        from porthole.updater.system import _FLAG
        flag = entry.get_text().strip().lstrip("+")
        if flag.startswith("-"):
            flag, on = flag[1:], False
        if not _FLAG.match(flag):
            entry.grab_focus()
            return
        if self._flag_state(flag) != on:
            self._toggle(flag)

    def _hardware(self, info):
        box, conf = self.tabs["hardware"], info["conf"]
        bar = self._changes_bar()
        if bar:
            box.pack_start(bar, False, False, 0)
        box.pack_start(_small(
            "What make.conf says about the hardware. Packages build support for exactly "
            "these."), False, False, 0)
        for name in ("VIDEO_CARDS", "INPUT_DEVICES", "CPU_FLAGS_X86"):
            var = conf["vars"].get(name)
            box.pack_start(_heading(name), False, False, 0)
            for note in (var or {}).get("notes") or []:
                box.pack_start(_small(note), False, False, 0)
            if not var:
                box.pack_start(_small("Not set in make.conf (the profile decides)."),
                               False, False, 0)
                continue
            values = info["expand"].get(name) or {}
            box.pack_start(_flags(var["text"].split(), values,
                                  set(values) or None), False, False, 0)
        cpu = info["machine"].get("cpu_flags")
        if cpu is not None:
            box.pack_start(_small("cpuid2cpuflags finds: " + " ".join(cpu)), False, False, 0)
            current = self._effective()["expand"].get("CPU_FLAGS_X86")
            written = (conf["vars"].get("CPU_FLAGS_X86") or {}).get("text", "").split()
            if current is not None:
                box.pack_start(_small("Porthole's file sets: " + " ".join(current)),
                               False, False, 0)
                button = Gtk.Button(label="Back to make.conf's CPU_FLAGS_X86",
                                    halign=Gtk.Align.START)
                button.connect("clicked", lambda b: self._set_expand("CPU_FLAGS_X86", None))
                box.pack_start(button, False, False, 0)
            elif sorted(cpu) != sorted(written):
                button = Gtk.Button(label="Use what cpuid2cpuflags finds",
                                    halign=Gtk.Align.START)
                button.set_tooltip_text("Set CPU_FLAGS_X86 to exactly what this CPU has, in "
                                        "Porthole's own file (make.conf stays as it is)")
                button.connect("clicked", lambda b: self._set_expand("CPU_FLAGS_X86", cpu))
                box.pack_start(button, False, False, 0)

    def _build(self, info):
        box, conf, m = self.tabs["build"], info["conf"], info["machine"]
        from porthole.updater.system import jobs_advice

        def value(name):
            return (conf["vars"].get(name) or {}).get("text") or "(not set)"
        box.pack_start(_heading("Compiler"), False, False, 0)
        box.pack_start(_facts([
            ("COMMON_FLAGS", value("COMMON_FLAGS")),
            ("CFLAGS", value("CFLAGS")),
            ("CXXFLAGS", value("CXXFLAGS")),
            ("-march=native here is", m.get("march_native") or "?")]), False, False, 0)
        box.pack_start(_heading("Parallel builds"), False, False, 0)
        jobs, suggested, why = jobs_advice(value("MAKEOPTS"), m)
        box.pack_start(_facts([("MAKEOPTS", value("MAKEOPTS")),
                               ("Suggested", "-j%d (%s)" % (suggested, why))]),
                       False, False, 0)


def _heading(text):
    label = Gtk.Label(xalign=0, margin_top=6)
    label.set_markup("<b>%s</b>" % GLib.markup_escape_text(text))
    return label


def _small(text, **props):
    label = Gtk.Label(xalign=0, wrap=True, max_width_chars=90, **props)
    label.set_markup("<small>%s</small>" % GLib.markup_escape_text(text))
    return label


def _facts(rows):
    grid = Gtk.Grid(column_spacing=12, row_spacing=4, margin_start=6)
    for i, (name, value) in enumerate(rows):
        grid.attach(Gtk.Label(label=name, xalign=0), 0, i, 1, 1)
        label = Gtk.Label(label=value, xalign=0, selectable=True, wrap=True)
        grid.attach(label, 1, i, 1, 1)
    return grid


def _text_colour():
    from porthole.utils.themecolours import hex_of, palette
    return hex_of(palette()["text"])


def _flags(flags, described, known, on_click=None, state=None, changed=(), pending=(),
           source=None, packages=None):
    """Flags as small labels that wrap, off ones dimmed, unknown ones
    struck through; hover for the description. With on_click, each is a
    button (state(flag): whether it is on now; changed: flags Porthole's
    file sets, marked; pending: not applied yet, in italics)"""
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=30,
                       column_spacing=4, row_spacing=0, margin_start=6, homogeneous=False)
    for flag in flags:
        name = flag.lstrip("-+")
        written_off = flag.startswith("-")
        off = (not state(name)) if state else written_off
        shown = ("-" if off else "") + name
        text = GLib.markup_escape_text(shown)
        if known is not None and name not in known:
            text = "<s>%s</s>" % text
        # one colour that means something: the theme's text, dimmed when off
        text = "<span foreground='%s'%s>%s</span>" % (
            _text_colour(), " alpha='55%'" if off else "", text)
        if name in pending:
            text = "<i>%s</i>" % text
        if name in changed:
            text += " \u2022"
        tip = described.get(name) or "No description"
        if known is not None and name not in known:
            tip = "No package has this flag: setting it changes nothing."
        tip = ("Off: " if off else "On: ") + tip
        if packages and name in packages:
            users = sorted(set(packages[name]))
            tip += "\nUsed by: %s%s" % (", ".join(users[:8]),
                                        " and %d more" % (len(users) - 8) if len(users) > 8 else "")
        if source:
            tip += "\n" + source(name) + "."
        elif name in changed:
            tip += "\nSet by Porthole's global USE file%s." % (
                " (not applied yet)" if name in pending else "")
        label = Gtk.Label(xalign=0)
        label.set_markup("<small>%s</small>" % text)
        if on_click:
            tip += "\nClick to turn it %s for every package." % ("on" if off else "off")
            # a plain label that takes clicks: button styling would colour
            # and space the flags like a toolbar
            box = Gtk.EventBox()
            box.add(label)
            box.set_tooltip_text(tip)
            box.connect("button-release-event",
                        lambda w, e, n=name: (on_click(n), True)[1] if e.button == 1 else False)
            box.connect("realize", lambda w: w.get_window().set_cursor(
                Gdk.Cursor.new_from_name(w.get_display(), "pointer")))
            flow.add(box)
        else:
            label.set_tooltip_text(tip)
            flow.add(label)
    return flow


class ProfileDialog(Gtk.Dialog):
    """Choose another profile: preview what it rebuilds, then switch
    (as root; Updates then works out the rebuild for review)"""

    def __init__(self, parent, current, on_switched):
        super().__init__(title="Porthole: change profile", transient_for=parent, modal=True)
        from porthole.updater import system
        self.current, self.on_switched = current, on_switched
        self.set_default_size(640, 520)
        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        area.pack_start(_small(
            "The profile sets this computer's defaults: USE flags, which packages make "
            "up the system, and the init system. Switching usually rebuilds a lot. "
            "Now: %s" % current), False, False, 0)
        self.store = Gtk.ListStore(str, str)
        self.all = system.profiles()
        self.view = Gtk.TreeView(model=self.store, headers_visible=True)
        for i, title in enumerate(("Profile", "Status")):
            self.view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.add(self.view)
        area.pack_start(scroller, True, True, 0)
        self.show_all_check = Gtk.CheckButton(label="Also show dev and exp profiles "
                                              "(not for everyday use)")
        self.show_all_check.connect("toggled", lambda b: self.fill())
        area.pack_start(self.show_all_check, False, False, 0)
        self.note = Gtk.Label(xalign=0, wrap=True, max_width_chars=80)
        area.pack_start(self.note, False, False, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.preview_button = self.add_button("Preview rebuild", 2)
        self.switch_button = self.add_button("Switch", Gtk.ResponseType.OK)
        self.view.get_selection().connect("changed", self.selected)
        self.connect("response", self.on_response)
        self.fill()
        self.show_all()

    def fill(self):
        self.store.clear()
        everything = self.show_all_check.get_active()
        for name, status, _dir in self.all:
            if everything or status == "stable" or name == self.current:
                row = self.store.append([name, status + (" (now)" if name == self.current
                                                         else "")])
                if name == self.current:
                    self.view.get_selection().select_iter(row)

    def chosen(self):
        model, row = self.view.get_selection().get_selected()
        return model[row][0] if row else None

    def selected(self, *args):
        from porthole.updater.system import init_system
        name = self.chosen()
        other = bool(name) and name != self.current
        self.preview_button.set_sensitive(other)
        self.switch_button.set_sensitive(other)
        notes = []
        if other and init_system(name) != init_system(self.current):
            notes.append("<b>This changes the init system (%s to %s).</b> That is more than "
                         "a rebuild: services, logging and boot change, and there are "
                         "manual steps (see the Gentoo handbook) before rebooting."
                         % (init_system(self.current), init_system(name)))
        parts = lambda n: (n or "").split("/")
        if other and "no-multilib" in parts(name) and "no-multilib" not in parts(self.current):
            notes.append("<b>Gentoo doesn't support switching to no-multilib in place:</b> "
                         "32-bit libraries the system has can't be removed this way. It "
                         "needs a reinstall from a no-multilib stage.")
        if other and ("hardened" in parts(name)) != ("hardened" in parts(self.current)):
            notes.append("This changes the compiler's default hardening: rebuild the "
                         "toolchain first, then everything (emerge -e @world), not just "
                         "what the update rebuilds.")
        status = next((s for n, s, _d in self.all if n == name), "")
        if other and status != "stable":
            notes.append("This is a %s profile: not tested for everyday use." % status)
        self.note.set_markup("<small>%s</small>" % "\n".join(notes))

    def on_response(self, dialog, response):
        if response == 2:
            self.preview()
        elif response == Gtk.ResponseType.OK:
            self.switch()
        else:
            self.destroy()

    def preview(self):
        import threading
        from porthole.updater import system
        name = self.chosen()
        self.set_sensitive(False)
        self.note.set_markup("<small>Working out what %s would rebuild, on a copy of the "
                             "configuration (a few minutes)...</small>"
                             % GLib.markup_escape_text(name))

        def work():
            try:
                plan = system.preview_profile(name)
                builds = [m["cpv"] for m in plan.get("merge_list") or []]
                if plan.get("status") in ("ready", "needs-approval", "up-to-date"):
                    text = ("Nothing needs rebuilding." if not builds else
                            "Would build %d package%s: %s" % (
                                len(builds), "" if len(builds) == 1 else "s",
                                ", ".join(builds[:40]) + (" ..." if len(builds) > 40 else "")))
                else:
                    from porthole.updater.analyze import summarize
                    title, body = summarize(plan)
                    text = "Portage couldn't work it out as it stands: %s %s" % (title, body)
            except Exception as e:
                text = "Couldn't preview it: %s" % e
            GLib.idle_add(done, text)

        def done(text):
            self.set_sensitive(True)
            self.selected()
            self.note.set_markup(self.note.get_label() + "\n<small>%s</small>"
                                 % GLib.markup_escape_text(text))
            return False
        threading.Thread(target=work, daemon=True).start()

    def switch(self):
        name = self.chosen()
        self.set_sensitive(False)

        def done(ok, output):
            self.set_sensitive(True)
            if not ok:
                _error(self, "The profile wasn't changed", output or "Cancelled.")
                return
            self.destroy()
            self.on_switched()
        run_as_root(service("profile", name), done)
