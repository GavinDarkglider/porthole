"""
    Porthole update assistant: running the update service as root

    Windows run as the user. Only submitting work (an update, a check,
    a settings change) needs root, and that goes through `sudo -A` so the
    sudoers rules decide: NOPASSWD means no prompt, and otherwise a small
    password dialog (porthole-askpass) asks. If sudo isn't allowed at all,
    pkexec is tried instead.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import shutil
import sys

from gi.repository import Gio, GLib

from porthole.updater.commands import LOCAL, SRC, script_command

# sudo's answers when it won't run the command for this user
_SUDO_REFUSED = ("not in the sudoers", "is not allowed to execute",
                 "not allowed to run sudo", "may not run sudo")


def askpass():
    """The password helper sudo -A runs"""
    if LOCAL:
        return os.path.join(SRC, "scripts", "porthole-askpass")
    return shutil.which("porthole-askpass") or "/usr/bin/porthole-askpass"


def service(*args):
    """argv for porthole-update-service with these arguments"""
    return script_command("porthole-update-service") + list(args)


def run_as_root(argv, callback):
    """Run argv as root without blocking; callback(ok, output) when done.
    While Porthole is unlocked its root helper runs it (no password)."""
    from porthole.updater.session import SESSION
    if SESSION.run(argv, callback):
        return
    if os.geteuid() == 0:
        _spawn(argv, callback)
    elif shutil.which("sudo"):
        env = {"SUDO_ASKPASS": askpass()}
        _spawn(["sudo", "-A", "--"] + argv, lambda ok, out: (
            callback(ok, out) if ok or not any(r in out for r in _SUDO_REFUSED)
            else _spawn(["pkexec"] + argv, callback)), env)
    else:
        _spawn(["pkexec"] + argv, callback)


def plan_as_user(request, callback, out=None):
    """Work out an install as the user (resolving needs no root):
    callback(plan_file, error). The plan is only for review; the runner
    plans again as root and installs only if it arrives at the same plan.
    While Porthole is unlocked it is worked out as root, which also sees
    the old libraries still in use (so @preserved-rebuild is in it)."""
    import json
    import tempfile
    from porthole.updater.commands import LOCAL, SRC
    from porthole.updater.session import SESSION
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                         "porthole", "plans")
    os.makedirs(cache, exist_ok=True)
    for old in sorted(os.listdir(cache))[:-20]:
        try:
            os.unlink(os.path.join(cache, old))
        except OSError:
            pass

    def planned_as_root(reply):
        if not reply["ok"]:
            callback(None, reply["error"].strip() or "The planner didn't produce a plan.")
            return
        path = out
        if not path:
            fd, path = tempfile.mkstemp(prefix="plan-", suffix=".json", dir=cache)
            os.close(fd)
        with open(path + ".new", "w") as f:
            f.write(reply["output"])
        os.replace(path + ".new", path)
        callback(path, None)
    if SESSION.service(["plan", "--request", json.dumps(request)], planned_as_root):
        return
    fd, request_file = tempfile.mkstemp(prefix="request-", suffix=".json", dir=cache)
    with os.fdopen(fd, "w") as f:
        json.dump(request, f)
    out = out or request_file.replace("request-", "plan-")
    if os.path.exists(out):
        os.unlink(out)  # so a failed run isn't mistaken for a new plan
    env = {"PYTHONPATH": SRC} if LOCAL else {}

    def done(ok, output):
        os.unlink(request_file)
        # the analyzer exits non-zero for plans needing help; the plan is there
        if os.path.exists(out):
            callback(out, None)
        else:
            callback(None, output or "The planner didn't produce a plan.")
    _spawn([sys.executable, "-m", "porthole.updater.analyze", "--json", out,
            "--request", request_file], done, env)


def run_set_config(args, on_done=None):
    """Run porthole's config file editor (backends/set_config.py) as root,
    for edits to make.conf and /etc/portage/package.* the user can't
    write. on_done() runs after a successful edit; a failure is shown."""
    from porthole.backends import set_config
    argv = [sys.executable, set_config.__file__] + list(args)

    def done(ok, output):
        if ok:
            if on_done:
                on_done()
            return
        from gi.repository import Gtk
        dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE,
                                   text="The change wasn't saved")
        dialog.set_title("Porthole")
        dialog.format_secondary_text(output or "Cancelled.")
        dialog.run()
        dialog.destroy()
    run_as_root(argv, done)


def _spawn(argv, callback, env=None):
    launcher = Gio.SubprocessLauncher.new(
        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
    for key, value in (env or {}).items():
        launcher.setenv(key, value, True)
    try:
        proc = launcher.spawnv(argv)
    except GLib.Error as e:
        GLib.idle_add(callback, False, str(e))
        return

    def done(proc, result):
        try:
            _ok, out, _err = proc.communicate_utf8_finish(result)
        except GLib.Error as e:
            callback(False, str(e))
            return
        callback(proc.get_successful(), (out or "").strip())
    proc.communicate_utf8_async(None, None, done)


def sync_now(parent=None, on_queued=None):
    """Sync the repositories and check for updates now, as a runner job.
    The mirrors ask for a sync at most once a day, so a sync within the
    last day is pointed out first. on_queued(job_id) runs once queued."""
    import time
    from gi.repository import Gtk
    from porthole.updater.service import last_repo_sync
    try:
        last = last_repo_sync()
    except Exception:
        last = None
    if last and time.time() - last < 24 * 3600:
        hours = (time.time() - last) / 3600
        dialog = Gtk.MessageDialog(transient_for=parent, modal=True,
                                   message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE,
                                   text="Sync again?")
        dialog.set_title("Porthole")
        dialog.format_secondary_text(
            "The package tree was synced %s ago. Gentoo's mirrors ask that you "
            "sync at most once a day; syncing more often can get you "
            "temporarily refused." % ("%d minutes" % (hours * 60) if hours < 1
                                      else "%.0f hours" % hours))
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Sync anyway", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            return

    def queued(ok, output):
        if not ok:
            dialog = Gtk.MessageDialog(transient_for=parent, modal=True,
                                       message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE,
                                       text="Couldn't start the sync")
            dialog.set_title("Porthole")
            dialog.format_secondary_text(output or "Cancelled.")
            dialog.run()
            dialog.destroy()
            return
        if on_queued:
            on_queued(output.strip().splitlines()[-1])
    run_as_root(service("submit", "check", "--sync-now"), queued)
