"""
    Porthole update assistant: unlocking Porthole for root work

    Porthole runs as the user; what changes the system goes through
    `porthole-update-service` as root, one sudo (or pkexec) per change.
    Unlocking asks for the password once and starts the service as a root
    helper for this Porthole:

        sudo -A porthole-update-service session --minutes N

    It talks only over its stdin and stdout (no socket anyone else could
    reach), one JSON object per line, and runs nothing of its own: each
    request is a run of the service (the verbs below, checked as always)
    or of the config file editor. It exits when Porthole closes its pipe
    (lock, quit or crash) and when the N minutes are up (0: never).

    While unlocked, the service's root-only views work too: plans include
    the old libraries still in use, and the Sets view shows
    @preserved-rebuild as it is now.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time

PROTOCOL = 1
# what the helper runs: the service's own verbs (it checks their arguments)
SERVICE_VERBS = {"submit", "cancel", "keep-building", "config", "news", "jobs", "plan",
                 "sets", "licences", "global-use", "profile"}
# read-only: stopped at once on lock, where the rest are let finish
READ_ONLY = {"jobs", "plan", "sets"}
# the unlock times Settings offers, in minutes (0: until Porthole closes)
TIMEOUTS = ((15, "15 minutes"), (60, "1 hour"), (0, "Until Porthole closes"))
DEFAULT_MINUTES = 15


def _set_config_script():
    return importlib.util.find_spec("porthole.backends.set_config").origin


# -- the root side ---------------------------------------------------------

def serve(minutes):
    """The helper's loop (porthole-update-service session, as root)"""
    from porthole.updater.commands import script_command
    deadline = time.monotonic() + minutes * 60 if minutes > 0 else None
    out_lock = threading.Lock()
    running = {}  # thread: (verb, Popen or None)
    requests = queue.Queue()

    def send(message):
        with out_lock:
            try:
                sys.stdout.write(json.dumps(message) + "\n")
                sys.stdout.flush()
            except OSError:
                pass

    def handle(request):
        reply = {"id": request.get("id"), "ok": False, "code": None,
                 "output": "", "error": ""}
        op, args = request.get("op"), request.get("args")
        if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
            reply["error"] = "Bad request"
        elif op == "service" and args[:2] == ["submit", "command"]:
            # a root command line is too big a prize to hand out on an unlock:
            # it always asks for the password itself (elevate.run_as_root fresh)
            reply["error"] = "Custom commands always ask for the password"
        elif op == "service" and args and args[0] in SERVICE_VERBS:
            argv = script_command("porthole-update-service") + args
        elif op == "set_config":
            argv = [sys.executable, _set_config_script()] + args
        else:
            reply["error"] = "Not something the root helper runs: %s" % (
                args[0] if op == "service" and args else op)
        if not reply["error"]:
            me = threading.current_thread()
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                reply["error"] = str(e)
            else:
                running[me] = (args[0] if op == "service" else op, proc)
                out, err = proc.communicate()
                reply.update(ok=proc.returncode == 0, code=proc.returncode,
                             output=out.decode(errors="replace"),
                             error=err.decode(errors="replace"))
        send(reply)
        running.pop(threading.current_thread(), None)

    def read_stdin():
        for line in sys.stdin:
            requests.put(line)
        requests.put(None)

    threading.Thread(target=read_stdin, daemon=True).start()
    send({"ready": True, "protocol": PROTOCOL, "minutes": minutes})
    while True:
        wait = None if deadline is None else deadline - time.monotonic()
        if wait is not None and wait <= 0:
            send({"locked": "time"})
            break
        try:
            line = requests.get(timeout=wait)
        except queue.Empty:
            continue
        if line is None:  # Porthole locked, closed or went away
            for verb, proc in list(running.values()):
                if verb in READ_ONLY:
                    proc.terminate()
            break
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError
        except ValueError:
            send({"id": None, "ok": False, "error": "Bad request"})
            continue
        thread = threading.Thread(target=handle, args=(request,))
        running[thread] = (None, None)
        thread.start()
    # let changes already asked for finish (a job half queued helps no one)
    for thread in list(running):
        thread.join()
    return 0


# -- Porthole's side -------------------------------------------------------

class Session:
    """The root helper of this Porthole, if unlocked. State changes are
    told to listeners (no arguments), in GTK's main loop."""

    def __init__(self):
        self.proc = None
        self.state = "locked"  # "unlocking", "unlocked"
        self.until = None  # wall time it locks itself, None: when Porthole closes
        self.pending = {}  # id: (callback, from_thread)
        self.next_id = 1
        self.mutex = threading.Lock()
        self.listeners = []
        self.waiting = []  # unlock callbacks
        self.errors = []

    def unlocked(self):
        return self.state == "unlocked"

    def minutes_left(self):
        """Whole minutes until it locks itself (rounded up), or None"""
        if not self.unlocked() or self.until is None:
            return None
        return max(0, int((self.until - time.time() + 59) // 60))

    def changed(self):
        for listener in list(self.listeners):
            listener()

    # -- unlocking and locking --

    def unlock(self, callback=None):
        """Ask for the password and start the helper; callback(ok, message)"""
        if callback:
            self.waiting.append(callback)
        if self.state == "unlocked":
            self._unlock_done(True, "")
            return
        if self.state == "unlocking":
            return
        import shutil
        from porthole.updater.elevate import askpass, service
        minutes = unlock_minutes()
        argv = service("session", "--minutes", str(minutes))
        self.state = "unlocking"
        self.changed()
        if os.geteuid() == 0:
            self._start(argv, {}, minutes, None)
        elif shutil.which("sudo"):
            self._start(["sudo", "-A", "--"] + argv, {"SUDO_ASKPASS": askpass()}, minutes,
                        ["pkexec"] + argv)
        else:
            self._start(["pkexec"] + argv, {}, minutes, None)

    def lock(self):
        """Lock now: the helper finishes what it was asked to change, then exits"""
        if self.proc:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        # its replies no longer matter: _ended tidies up when it has gone

    def _start(self, argv, env, minutes, fallback):
        # while the helper's pipe is open, nothing else running as this user
        # may attach to Porthole or open its file descriptors through /proc
        set_dumpable(False)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=dict(os.environ, **env),
                                    # no terminal for sudo to put it in
                                    start_new_session=True)
        except OSError as e:
            self._ended(None, str(e), None)
            return
        self.proc = proc
        errors = []
        threading.Thread(target=lambda: errors.extend(
            proc.stderr.read().decode(errors="replace").splitlines()), daemon=True).start()
        threading.Thread(target=self._read, args=(proc, errors, minutes, fallback),
                         daemon=True).start()

    def _read(self, proc, errors, minutes, fallback):
        """The helper's replies (a thread of its own)"""
        from gi.repository import GLib
        for raw in proc.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("ready"):
                if message.get("protocol") != PROTOCOL:
                    errors.append("The root helper is a different version: "
                                  "restart Porthole after updating it.")
                    proc.stdin.close()
                    continue
                GLib.idle_add(self._ready, proc, minutes)
            elif "id" in message:
                with self.mutex:
                    callback, from_thread = self.pending.pop(message["id"], (None, False))
                if callback and from_thread:
                    callback(message)
                elif callback:
                    GLib.idle_add(callback, message)
        proc.wait()
        GLib.idle_add(self._ended, proc, "\n".join(errors), fallback)

    def _ready(self, proc, minutes):
        if proc is not self.proc:
            return False
        self.state = "unlocked"
        self.until = time.time() + minutes * 60 if minutes else None
        self._unlock_done(True, "")
        self.changed()
        return False

    def _ended(self, proc, errors, fallback):
        if proc is not None and proc is not self.proc:
            return False
        was = self.state
        self.proc, self.state, self.until = None, "locked", None
        set_dumpable(True)
        with self.mutex:
            orphans, self.pending = self.pending, {}
        for callback, _from_thread in orphans.values():
            callback({"ok": False, "output": "", "error": "Porthole was locked before "
                      "it finished; nothing more was done."})
        if was == "unlocking":
            from porthole.updater.elevate import _SUDO_REFUSED
            if fallback and any(r in errors for r in _SUDO_REFUSED):
                self.state = "unlocking"
                self._start(fallback, {}, unlock_minutes(), None)
                return False
            self._unlock_done(False, errors.strip() or "Cancelled.")
        self.changed()
        return False

    def _unlock_done(self, ok, message):
        waiting, self.waiting = self.waiting, []
        for callback in waiting:
            callback(ok, message)

    # -- running things as root --

    def _send(self, op, args, callback, from_thread):
        with self.mutex:
            proc = self.proc
            if not self.unlocked() or proc is None:
                return False
            request_id = self.next_id
            self.next_id += 1
            self.pending[request_id] = (callback, from_thread)
            try:
                proc.stdin.write((json.dumps({"id": request_id, "op": op, "args": args})
                                  + "\n").encode())
                proc.stdin.flush()
            except (OSError, ValueError):
                del self.pending[request_id]
                return False
        return True

    def run(self, argv, callback):
        """run_as_root through the helper, if unlocked and it is something
        the helper runs: callback(ok, output) in the main loop. False if
        not (then sudo it is)."""
        from porthole.updater.elevate import service
        prefix = service()
        if argv[:len(prefix)] == prefix and argv[len(prefix):]:
            op, args = "service", argv[len(prefix):]
        elif argv[:2] == [sys.executable, _set_config_script()]:
            op, args = "set_config", argv[2:]
        else:
            return False
        return self._send(op, args, lambda reply: callback(
            reply["ok"], (reply["output"] + "\n" + reply["error"]).strip()), False)

    def service(self, args, callback):
        """porthole-update-service ARGS as root; callback(reply) in the
        main loop, reply: ok, output (stdout), error (stderr)"""
        return self._send("service", list(args), callback, False)

    def service_wait(self, args, timeout=None):
        """The same, waiting for the reply (not in the main loop). None if
        locked."""
        done = threading.Event()
        replies = []
        if not self._send("service", list(args),
                          lambda reply: (replies.append(reply), done.set()), True):
            return None
        done.wait(timeout)
        return replies[0] if replies else None


SESSION = Session()


def set_dumpable(on):
    """prctl(PR_SET_DUMPABLE): off, /proc/PID of this process is root's and
    other processes (even this user's) can't ptrace it or open its fds"""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 1 if on else 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE
    except (OSError, AttributeError):
        return False


def unlocked():
    return SESSION.unlocked()


def unlock_minutes():
    """How long an unlock lasts, from Porthole's settings"""
    try:
        from porthole import config
        minutes = int(config.Prefs.globals.unlock_minutes)
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_MINUTES
    return minutes if minutes in dict(TIMEOUTS) else DEFAULT_MINUTES


def root_sets(names):
    """{name: [cat/pkg...]} for root-only sets, worked out now as root if
    unlocked; None if locked (or it failed)"""
    if not unlocked():
        return None
    reply = SESSION.service_wait(["sets"] + list(names), timeout=300)
    if not reply or not reply["ok"]:
        return None
    try:
        return json.loads(reply["output"])
    except ValueError:
        return None


# -- the Unlock/Lock button ------------------------------------------------

def lock_button(tool=False, parent=None):
    """A button that unlocks Porthole and locks it again, showing the time
    left: a toolbar button (tool) or a plain one"""
    from gi.repository import GLib, Gtk
    if tool:
        button = Gtk.ToolButton()
        button.set_is_important(True)
        image = None
    else:
        button = Gtk.Button()
        image = Gtk.Image()
        button.set_image(image)
        button.set_always_show_image(True)

    def update():
        state = SESSION.state
        icon = "changes-allow-symbolic" if state == "unlocked" else "changes-prevent-symbolic"
        if state == "unlocked":
            left = SESSION.minutes_left()
            label = "Lock" if left is None else "Lock (%d min)" % left
            tip = ("Porthole is unlocked: changes and custom commands run as root "
                   "without asking for the password again%s. Click to lock it now."
                   % ("" if left is None else ", for %d more minute%s"
                      % (left, "" if left == 1 else "s")))
        elif state == "unlocking":
            label, tip = "Unlocking...", "Waiting for the password"
        else:
            label = "Unlock"
            tip = ("Enter the password once, for a while, instead of for every "
                   "change. While unlocked, checks also see old libraries still in "
                   "use, and custom commands can run as root.")
        if tool:
            button.set_icon_name(icon)
        else:
            image.set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        button.set_label(label)
        button.set_tooltip_text(tip)
        button.set_sensitive(state != "unlocking")

    def clicked(widget):
        if SESSION.unlocked():
            SESSION.lock()
        else:
            SESSION.unlock(lambda ok, message: ok or unlock_failed(
                widget.get_toplevel(), message))

    def tick():
        update()
        return True

    button.connect("clicked", clicked)
    SESSION.listeners.append(update)
    timer = GLib.timeout_add_seconds(15, tick)

    def destroyed(widget):
        GLib.source_remove(timer)
        if update in SESSION.listeners:
            SESSION.listeners.remove(update)
    button.connect("destroy", destroyed)
    update()
    return button


def unlock_failed(parent, message):
    from gi.repository import Gtk
    dialog = Gtk.MessageDialog(transient_for=parent if isinstance(parent, Gtk.Window)
                               else None, modal=True, message_type=Gtk.MessageType.ERROR,
                               buttons=Gtk.ButtonsType.CLOSE, text="Porthole wasn't unlocked")
    dialog.set_title("Porthole")
    dialog.format_secondary_text(message)
    dialog.run()
    dialog.destroy()
