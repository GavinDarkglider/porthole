"""
    Porthole update assistant: following an update job

    Prints a job's log (as written by the root job runner) and keeps
    printing until the job ends, then exits 0 if it succeeded and 1 if
    not. Porthole's terminal runs this, so the job gets the usual
    colouring, warnings and cautions tabs and summary; closing the
    terminal only stops following, never the job.

    Usage: python3 -m porthole.updater.follow [JOB_ID]   (default: current job)

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import re
import shlex
import signal
import sys
import time

from porthole.updater.runner import ACTIVE, JOBS_FILE
from porthole.updater.service import read_json

# start this far from the end of a long log (emerge build output is big)
REPLAY_BYTES = 8 * 1024 * 1024
# the one process showing jobs in a terminal window, for this user
PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR")
                       or os.path.expanduser("~/.cache/porthole"), "porthole-follow.pid")
_FOLLOW = re.compile(r"porthole\.updater\.follow(?: (\S+))?\s*$")


def find(job_id=None):
    jobs = (read_json(JOBS_FILE, {}) or {}).get("jobs", [])
    if job_id:
        return next((j for j in jobs if j["id"] == job_id), None)
    for status in ("running", "queued"):
        for job in jobs:
            if job["status"] == status:
                return job
    return jobs[-1] if jobs else None


def follow(job_id=None, out=None):
    out = out or sys.stdout.buffer
    job = find(job_id)
    if not job:
        out.write(b"No update jobs yet.\n")
        return 0
    job_id = job["id"]
    out.write(("*** Following %s: %s (closing this window doesn't stop it)\n"
               % (job_id, job["title"])).encode())
    out.flush()
    # a queued job has no log yet
    while job and job["status"] == "queued" and not os.path.exists(job["log"]):
        time.sleep(1)
        job = find(job_id)
    with open(job["log"], "rb") as log:
        size = os.fstat(log.fileno()).st_size
        if size > REPLAY_BYTES:
            log.seek(size - REPLAY_BYTES)
            log.readline()  # from a line start
            out.write(b"*** (earlier output not shown)\n")
        while True:
            data = log.read(65536)
            if data:
                out.write(data)
                out.flush()
                continue
            job = find(job_id)
            if not job or job["status"] not in ACTIVE:
                # the runner writes its last lines before the final status
                data = log.read()
                if data:
                    out.write(data)
                break
            time.sleep(0.5)
    status = (job or {}).get("status", "gone")
    out.write(("\n*** Job %s\n" % status).encode())
    out.flush()
    return 0 if status == "done" else 1


def command(job_id=None):
    """Shell command for Porthole's terminal to follow a job"""
    import shlex
    from porthole.updater.commands import LOCAL, SRC
    argv = [sys.executable, "-m", "porthole.updater.follow"] + ([job_id] if job_id else [])
    prefix = "PYTHONPATH=%s " % shlex.quote(SRC) if LOCAL else ""
    return prefix + shlex.join(argv)


def display_command(command_string):
    """What the terminal should show for a command: for a follower, the
    job's own command line (or its title), not the python running it"""
    match = _FOLLOW.search(command_string or "")
    if not match:
        return command_string
    job = find(match.group(1))
    if not job:
        return command_string
    steps = job.get("steps") or []
    if len(steps) == 1 and steps[0].get("argv"):
        env = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in
                       sorted((steps[0].get("env") or {}).items()))
        return (env + " " if env else "") + shlex.join(steps[0]["argv"])
    return job["title"]


def job_of(command_string):
    """The id of the job a follower command follows, or None"""
    match = _FOLLOW.search(command_string or "")
    if not match:
        return None
    job = find(match.group(1))
    return job["id"] if job else None


def running_follower():
    """pid of the process showing jobs in a terminal window, if any"""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            if b"porthole" not in f.read():
                return None
    except (OSError, ValueError):
        return None
    return pid


def raise_existing():
    """Ask the process already showing jobs to bring its window up (and
    follow the current job). True if there was one."""
    pid = running_follower()
    if pid is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        return False
    return True


class Follower:
    """The one terminal window following jobs, in this process"""

    def __init__(self, on_close=None):
        self.manager = None
        self.followed = set()
        self.on_close = on_close
        from gi.repository import GLib
        # another launch asks us to show the window instead of opening one
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                             lambda: (self.show(), True)[1])

    def show(self, job_id=None):
        """Bring the window up, following job_id (default: the current job)"""
        from porthole.terminal.terminal import ProcessManager
        from porthole.utils import utils
        if self.manager is None or not self.manager.window_visible:
            self.manager = ProcessManager(utils.environment())
            self.followed = set()
        job = find(job_id)
        new_window = not self.followed
        if job and job["id"] not in self.followed:
            self.followed.add(job["id"])
            self.manager.add(job["title"], command(job["id"]), lambda: None,
                             "Job runner")
            if new_window:
                self.manager.window.connect("destroy", self.closed)
        self.manager.window.present()
        os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))

    def closed(self, window):
        if running_follower() == os.getpid():
            try:
                os.unlink(PIDFILE)
            except OSError:
                pass
        if self.on_close:
            self.on_close()


def show_in_terminal(job_id=None, on_close=None):
    """Follow a job in Porthole's job terminal window, opening it unless
    one is already open in this or another process (Porthole's
    preferences must be loaded). Returns this process's Follower, or None
    if another process shows it."""
    global _follower
    if raise_existing():
        return None
    if _follower is None:
        _follower = Follower(on_close)
    _follower.show(job_id)
    return _follower


_follower = None


if __name__ == "__main__":
    sys.exit(follow(sys.argv[1] if len(sys.argv) > 1 else None))
