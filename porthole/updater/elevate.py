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
    """Run argv as root without blocking; callback(ok, output) when done"""
    if os.geteuid() == 0:
        _spawn(argv, callback)
    elif shutil.which("sudo"):
        env = {"SUDO_ASKPASS": askpass()}
        _spawn(["sudo", "-A", "--"] + argv, lambda ok, out: (
            callback(ok, out) if ok or not any(r in out for r in _SUDO_REFUSED)
            else _spawn(["pkexec"] + argv, callback)), env)
    else:
        _spawn(["pkexec"] + argv, callback)


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
