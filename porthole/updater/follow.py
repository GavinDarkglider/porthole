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
    if job["status"] in ACTIVE:
        out.write(("*** Following %s: %s (closing this window doesn't stop it)\n"
                   % (job_id, job["title"])).encode())
    else:
        out.write(("*** %s: %s already finished (%s) at %s. This is its log; "
                   "nothing is running.\n"
                   % (job_id, job["title"], job["status"],
                      time.strftime("%Y-%m-%d %H:%M", time.localtime(
                          job.get("finished") or 0)))).encode())
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
            start = log.tell()
            # the terminal's warnings, cautions and messages tabs still get
            # everything: from the part not shown, its lines for those tabs
            out.write(b"*** (earlier output not shown; its warnings and messages:)\n")
            out.flush()
            log.seek(0)
            count = earlier_messages(log, start, out)
            out.write(b"*** (end of the earlier %d lines; the output from here on:)\n" % count)
            log.seek(start)
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
    if (job and job.get("kind") == "install" and status in ("failed", "cancelled")
            and job.get("keep_rest", True)):
        out.write(b"*** What it didn't get to goes back in Porthole's install queue: "
                  b"process the queue to carry on (packages built meanwhile are "
                  b"left out), or remove it there.\n")
    if job and status == "done" and _removes(job):
        out.write(b"*** What the removed packages needed may not be needed any more: "
                  b"the update assistant lists such packages after a check (Check now), "
                  b"to remove the ones you tick.\n")
    out.flush()
    return 0 if status == "done" else 1


def _filters():
    """Porthole's terminal's line filters (config/configuration.xml), or
    None when they can't be found"""
    import contextlib
    import io
    # Porthole's modules print debug lines as they load: not into the log
    with contextlib.redirect_stdout(io.StringIO()):
        from porthole import config
        if getattr(config.Config, "emerge_re", None) is None:
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"
            for data_path in (here, "/usr/share/porthole/"):
                if os.path.exists(data_path + "config/configuration.xml"):
                    config.Config.set_path(data_path)
                    config.Config.load()
                    break
            else:
                return None
    return config.Config


def earlier_messages(log, end, out):
    """Write the lines of log (from where it is to byte end) that the
    terminal shows in its warnings, cautions or messages tabs, each after
    the ">>> Emerging" line it came under. Returns how many."""
    from porthole.updater.runner import ANSI
    filters = _filters()
    if filters is None:
        return 0
    tests = (filters.isInfo, filters.isWarning, filters.isCaution, filters.isAction)
    count, emerging = 0, None
    while log.tell() < end:
        raw = log.readline()
        if not raw:
            break
        # what the line ends up as on screen: colours gone, and only the
        # last of what carriage returns wrote over
        line = ANSI.sub(b"", raw).rstrip(b"\r\n").rsplit(b"\r", 1)[-1]
        text = line.decode("utf-8", "replace")
        if filters.isEmerge(text):
            emerging = line
            continue
        if any(test(text) for test in tests):
            if emerging is not None:
                out.write(emerging + b"\n")
                emerging = None
            out.write(line + b"\n")
            count += 1
    out.flush()
    return count


def _removes(job):
    """Whether a job unmerged packages (not a depclean, which cleans up itself)"""
    for step in job.get("steps") or []:
        argv = step.get("argv") or []
        if "--unmerge" in argv or "-C" in argv or "--rage-clean" in argv:
            return True
    return False


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
    for step in job.get("steps") or []:
        if step.get("argv"):
            env = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in
                           sorted((step.get("env") or {}).items()))
            return (env + " " if env else "") + shlex.join(
                [a for a in step["argv"] if a != "--color=y"])
        # a plan stage: its emerge command(s), without the shell around them
        commands = [c.strip() for c in step.get("shell", "").split(";")[-1].split("&&")
                    if c.strip().startswith("emerge")]
        if commands:
            return " && ".join(c.replace(" --color=y", "") for c in commands)
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


def raise_existing(job_id=None):
    """Ask the process already showing jobs to bring its window up and
    follow job_id (default: the current job). True if there was one."""
    pid = running_follower()
    if pid is None or pid == os.getpid():
        return False
    try:
        with open(PIDFILE + ".want", "w") as f:
            f.write(job_id or "")
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        return False
    return True


def wanted_job():
    """The job another launch asked us to follow (see raise_existing)"""
    try:
        with open(PIDFILE + ".want") as f:
            return f.read().strip() or None
    except OSError:
        return None


class Follower:
    """The one terminal window following jobs, in this process"""

    def __init__(self, on_close=None):
        self.manager = None
        self.latest = None
        self.followed = set()
        # jobs that were still to finish when this window started following
        # them: only then does it close by itself once they all went well
        self.watched = set()
        self.on_close = on_close
        from gi.repository import GLib
        # another launch asks us to show the window instead of opening one
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                             lambda: (self.show(wanted_job()), True)[1])

    def show(self, job_id=None):
        """Bring the window up, following job_id (default: the current job)"""
        from porthole.terminal.terminal import ProcessManager
        from porthole.utils import utils
        if self.manager is None or not self.manager.window_visible:
            self.manager = ProcessManager(utils.environment())
            self.manager.job_window()
            self.followed = set()
            self.watched = set()
            self.latest = None
        job = find(job_id)
        if job:
            self.latest = job["id"]
        new_window = not self.followed
        if job and job["id"] not in self.followed:
            self.followed.add(job["id"])
            if job["status"] in ACTIVE:
                self.watched.add(job["id"])
            self.manager.add(job["title"], command(job["id"]), lambda: None,
                             "Job runner")
            if new_window:
                self.manager.window.connect("destroy", self.closed)
        if new_window:
            self.add_paused_bar()
            self.add_closing_bar()
            self.add_commands_tab()
        self.manager.window.present()
        os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))

    def add_commands_tab(self):
        """The Commands tab: the command record of the job selected in the
        queue, else of the job followed last"""
        from porthole.updater.commandtab import CommandsTab

        def job_for():
            queue = getattr(self.manager, "process_queue", None)
            selected = queue.selected_job() if queue is not None else None
            return find(selected or self.latest) if (selected or self.latest) else None
        self.commands_tab = CommandsTab(self.manager.notebook, job_for)

    def add_paused_bar(self):
        """A bar while a followed build is paused for someone at the computer,
        with Keep building: watching a build shouldn't stop it"""
        from gi.repository import GLib, Gtk
        bar = Gtk.InfoBar(message_type=Gtk.MessageType.INFO)
        label = Gtk.Label(xalign=0, wrap=True)
        bar.get_content_area().add(label)
        bar.add_button("Keep building", 1)
        bar.set_no_show_all(True)
        box = self.manager.wtree.get_object("vbox3")
        box.pack_start(bar, False, False, 0)
        box.reorder_child(bar, 1)  # under the menu bar
        state = {"job": None}

        def keep(widget, response):
            from porthole.updater.elevate import run_as_root, service
            bar.set_sensitive(False)
            run_as_root(service("keep-building", state["job"]),
                        lambda ok, out: (bar.set_sensitive(True), refresh()))
        bar.connect("response", keep)

        def refresh():
            if self.manager is None or not self.manager.window_visible:
                return False
            jobs = (read_json(JOBS_FILE, {}) or {}).get("jobs", [])
            paused = next((j for j in jobs if j["id"] in self.followed
                           and j["status"] == "running" and j.get("paused")), None)
            if paused:
                from porthole.updater.service import load_config
                minutes = max(load_config()["idle_minutes"], 1)
                state["job"] = paused["id"]
                label.set_markup(
                    "<b>Paused because the computer is in use.</b> It carries on once "
                    "nobody has used it for %d minute%s. Watching it here counts as "
                    "using it: choose Keep building to let it run while you're here."
                    % (minutes, "" if minutes == 1 else "s"))
                label.show()
                bar.show()
            else:
                bar.hide()
            return True
        refresh()
        GLib.timeout_add_seconds(3, refresh)

    CLOSE_AFTER = 10

    def add_closing_bar(self):
        """Once every job this window watched has finished well and its
        queue has nothing left: closing in CLOSE_AFTER seconds, unless Keep
        open. A job that failed or was cancelled keeps it open."""
        from gi.repository import GLib, Gtk
        bar = Gtk.InfoBar(message_type=Gtk.MessageType.INFO)
        label = Gtk.Label(xalign=0, wrap=True)
        bar.get_content_area().add(label)
        bar.add_button("Keep open", 1)
        bar.set_no_show_all(True)
        box = self.manager.wtree.get_object("vbox3")
        box.pack_start(bar, False, False, 0)
        box.reorder_child(bar, 1)
        manager = self.manager
        state = {"left": None, "keep": False}

        def keep(widget, response):
            state["keep"] = True
            bar.hide()
        bar.connect("response", keep)

        def tick():
            if self.manager is not manager or not manager.window_visible:
                return False
            jobs = {j["id"]: j for j in (read_json(JOBS_FILE, {}) or {}).get("jobs", [])}
            watched = [jobs.get(job_id) for job_id in self.watched]
            finished_well = (bool(watched) and not state["keep"]
                             and all(j and j["status"] == "done" for j in watched)
                             and manager.task_completed
                             and not getattr(manager.process_queue, "process_iter", None))
            if not finished_well:
                state["left"] = None
                bar.hide()
                return True
            if state["left"] is None:
                state["left"] = self.CLOSE_AFTER
            if state["left"] <= 0:
                manager.confirm_delete()
                return False
            label.set_markup("<b>Finished.</b> This window closes in %d s." % state["left"])
            label.show()
            bar.show()
            state["left"] -= 1
            return True
        GLib.timeout_add_seconds(1, tick)

    def closed(self, window):
        if running_follower() == os.getpid():
            try:
                os.unlink(PIDFILE)
            except OSError:
                pass
        if self.on_close:
            self.on_close()


# set by Porthole when it opens the job window, for the job to follow
JOB_ENV = "PORTHOLE_FOLLOW_JOB"


def show_in_terminal(job_id=None):
    """Bring up the job window following job_id (default: the current
    job). It is a process of its own ('porthole --follow-job'), so closing
    Porthole doesn't close it, and there is only ever one."""
    if raise_existing(job_id):
        return
    import subprocess
    from porthole.updater.commands import script_command
    env = dict(os.environ)
    if job_id:
        env[JOB_ENV] = job_id
    subprocess.Popen(script_command("porthole") + ["--follow-job"], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def run_window(on_close):
    """For 'porthole --follow-job': show the window here, unless another
    process already does (it is asked to come up). False if so."""
    job_id = os.environ.get(JOB_ENV)
    if raise_existing(job_id):
        return False
    Follower(on_close).show(job_id)
    return True


if __name__ == "__main__":
    sys.exit(follow(sys.argv[1] if len(sys.argv) > 1 else None))
