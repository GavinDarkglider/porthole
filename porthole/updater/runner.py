"""
    Porthole update job runner

    Runs update work as root so no window has to stay open for it. Jobs
    are submitted with `porthole-update-service submit ...` (through sudo
    or pkexec) and queued in

        /var/lib/porthole/jobs.json       queue, progress, results (world readable)
        /var/log/porthole/jobs/<id>.log   output of each job (world readable)

    A runner process is started for the queue and exits once it is empty.
    Only known kinds of job are accepted, never arbitrary commands: an
    update must be the exact plan the user reviewed, and is refused if
    the plan has gone stale. Whatever happens to a job (done, failed,
    cancelled), the plan is checked again afterwards, so the tray and the
    update assistant always show what is left.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import contextlib
import datetime
import fcntl
import os
import re
import shlex
import signal
import subprocess
import time

from porthole.updater.analyze import CONFIG_DIRS, OWNED_NAME, TEMP_NAME
from porthole.updater.commands import script_command
from porthole.updater.service import (
    CHOICES_FILE, CONFIG_FILE, DEFAULTS, PLAN_FILE, STATE_DIR, Log, Service, check_lock,
    last_repo_sync, load_config, read_json, write_json)

PORTAGE_DIR = "/etc/portage"
JOBS_FILE = os.path.join(STATE_DIR, "jobs.json")
JOB_LOG_DIR = "/var/log/porthole/jobs"
RUNNER_LOCK = "/run/lock/porthole-runner.lock"
JOBS_LOCK = "/run/lock/porthole-jobs.lock"
# finished jobs (and their logs) kept for the record
KEEP_JOBS = 20
# changes this close to the check were made by the check itself
GRACE = 60
# write progress to jobs.json at most this often
PROGRESS_EVERY = 2.0

ACTIVE = ("queued", "running")
EMERGING = re.compile(rb">>> Emerging (?:binary )?\((\d+) of (\d+)\) (\S+)")


class JobError(Exception):
    """A job that can't be accepted; the message is for the user"""


# -- plans -----------------------------------------------------------------

def plan_time(plan):
    try:
        return datetime.datetime.fromisoformat(plan["generated"]).timestamp()
    except (KeyError, ValueError):
        return 0


def newest_config_change(top=PORTAGE_DIR):
    newest = 0
    for dirpath, dirnames, filenames in os.walk(top):
        for name in dirnames + filenames:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                pass
    return newest


def stale_reason(plan):
    """Why the plan may no longer match the system, or None"""
    checked = plan_time(plan)
    if (last_repo_sync() or 0) > checked + GRACE:
        return "The repositories were synced after this check."
    if newest_config_change() > checked + GRACE:
        return "/etc/portage was changed after this check."
    return None


def write_fixes(fixes):
    """Append approved lines to /etc/portage/package.*/zz-porthole"""
    for fix in fixes:
        path = os.path.join(PORTAGE_DIR, CONFIG_DIRS[fix["kind"]])
        if os.path.isfile(path):
            raise JobError(
                "%s is a single file. Porthole keeps its changes in its own file, "
                "so it needs %s to be a directory. Move the file into a new "
                "directory of that name and re-check." % (path, path))
    stamp = time.strftime("%Y-%m-%d")
    written = []
    for fix in fixes:
        directory = os.path.join(PORTAGE_DIR, CONFIG_DIRS[fix["kind"]])
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, OWNED_NAME)
        try:
            with open(path) as f:
                existing = f.read().splitlines()
        except FileNotFoundError:
            existing = None
        if existing and fix["line"] in existing:
            continue
        with open(path, "a") as f:
            if existing is None:
                f.write("# Added by Porthole's update assistant. "
                        "Safe to edit or remove.\n")
            reason = (fix.get("explanation") or [""])[0]
            f.write("\n# %s: %s\n%s\n" % (stamp, reason, fix["line"]))
        written.append((path, fix["line"]))
    return written


def stage_shell(stage):
    """One shell command line for a stage; stops at the first failure"""
    temp = os.path.join(PORTAGE_DIR, CONFIG_DIRS["use"], TEMP_NAME)
    steps = []
    if stage.get("remove_config"):
        steps.append("rm -f " + shlex.quote(temp))
    if stage.get("config"):
        steps.append("printf '%s\\n' " + " ".join(shlex.quote(line) for line in
                     stage["config"]["use"]) + " > " + shlex.quote(temp))
    steps.extend(shlex.join(command) for command in stage["commands"])
    # pick up a compiler switched by an earlier stage
    return ". /etc/profile >/dev/null 2>&1; " + " && ".join(steps)


# -- the job list ----------------------------------------------------------

@contextlib.contextmanager
def jobs_state(write=True):
    """The job list, locked against other submitters and the runner"""
    os.makedirs(os.path.dirname(JOBS_LOCK), exist_ok=True)
    with open(JOBS_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(JOBS_FILE, {}) or {}
        state.setdefault("jobs", [])
        yield state
        if write:
            write_json(JOBS_FILE, state)


def find_job(state, job_id):
    for job in state["jobs"]:
        if job["id"] == job_id:
            return job
    return None


def active_jobs(state):
    return [job for job in state["jobs"] if job["status"] in ACTIVE]


def requested_by():
    """Who asked, for the record (sudo and pkexec both say)"""
    if os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    uid = os.environ.get("PKEXEC_UID")
    if uid:
        try:
            import pwd
            return pwd.getpwuid(int(uid)).pw_name
        except (KeyError, ValueError):
            return "uid " + uid
    return "root"


def new_job(kind, title, steps, recheck, **extra):
    now = time.time()
    # several jobs can be submitted within a second
    job_id = "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)), kind,
                           os.urandom(2).hex())
    job = {
        "id": job_id,
        "kind": kind,
        "title": title,
        "status": "queued",
        "submitted": now,
        "by": requested_by(),
        "log": os.path.join(JOB_LOG_DIR, job_id + ".log"),
        "steps": [dict(step, status="queued") for step in steps],
        # check the plan again when the job ends, whatever happened
        "recheck": recheck,
        "progress": None,
    }
    job.update(extra)
    return job


def prune(state):
    """Keep the last KEEP_JOBS finished jobs and their logs"""
    finished = [job for job in state["jobs"] if job["status"] not in ACTIVE]
    for job in finished[:-KEEP_JOBS]:
        state["jobs"].remove(job)
        try:
            os.unlink(job["log"])
        except OSError:
            pass


# -- submitting ------------------------------------------------------------

def submit_update(generated):
    """Queue the reviewed plan; `generated` names the plan the user saw"""
    plan = read_json(PLAN_FILE)
    if not plan:
        raise JobError("There is no update plan. Check for updates first.")
    if plan.get("generated") != generated:
        raise JobError("The plan changed after you reviewed it. Review it again.")
    if plan.get("status") not in ("ready", "needs-approval"):
        raise JobError("This plan can't be run automatically (%s)." % plan.get("status"))
    stale = stale_reason(plan)
    if stale:
        raise JobError(stale + " Check again before updating.")

    steps = []
    if plan.get("fixes"):
        steps.append({"kind": "apply-config",
                      "title": "Add the approved changes to /etc/portage",
                      "fixes": plan["fixes"]})
    for stage in plan.get("stages") or []:
        steps.append({"kind": "shell", "title": stage["title"],
                      "shell": stage_shell(stage)})
    count = len(plan.get("merge_list") or []) + len(
        (plan.get("preserved") or {}).get("merge_list") or [])
    job = new_job("update", "Update %d package%s" % (count, "s" if count != 1 else ""),
                  steps, recheck=True, plan=generated)
    with jobs_state() as state:
        if any(j["kind"] == "update" for j in active_jobs(state)):
            raise JobError("An update is already queued or running.")
        state["jobs"].append(job)
        prune(state)
    return job


def submit_check(sync=True):
    """Queue an update check (syncing first, at most once a day)"""
    job = new_job("check", "Check for updates",
                  [{"kind": "check", "title": "Sync and work out an update plan"
                    if sync else "Work out an update plan", "sync": sync}],
                  recheck=False)
    with jobs_state() as state:
        if any(j["kind"] == "check" and j["status"] == "queued" for j in state["jobs"]):
            raise JobError("A check is already queued.")
        state["jobs"].append(job)
        prune(state)
    return job


def cancel(job_id=None):
    """Cancel queued jobs and stop the running one (or just job_id)"""
    stopped = []
    with jobs_state() as state:
        for job in active_jobs(state):
            if job_id and job["id"] != job_id:
                continue
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["finished"] = time.time()
                for step in job["steps"]:
                    step["status"] = "not run"
            else:
                job["cancel"] = True
                pid = job.get("pid")
                if pid:
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except OSError:
                        pass
            stopped.append(job["id"])
    return stopped


def start_runner():
    """Start a runner unless one is going; it outlives this process"""
    subprocess.Popen(script_command("porthole-update-service") + ["runner"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     close_fds=True)


def runner_active():
    """True while a runner holds its lock"""
    try:
        with open(RUNNER_LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


# -- running ---------------------------------------------------------------

class JobLog(Log):
    """Lines for both the job's log and the updater log"""

    def __init__(self, path):
        super().__init__(echo=False)
        self.job_file = open(path, "ab")

    def __call__(self, msg):
        super().__call__(msg)
        self.write(("%s\n" % msg).encode())

    def write(self, data):
        self.job_file.write(data)
        self.job_file.flush()


class Runner:
    def __init__(self):
        self.lock = None

    def run(self):
        os.makedirs(os.path.dirname(RUNNER_LOCK), exist_ok=True)
        self.lock = open(RUNNER_LOCK, "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # the running one will get to the queue
        os.makedirs(JOB_LOG_DIR, mode=0o755, exist_ok=True)
        while True:
            with jobs_state() as state:
                queued = [j for j in state["jobs"] if j["status"] == "queued"]
                if not queued:
                    # let go while the list is still locked, so a job
                    # submitted now finds the lock free for its own runner
                    state.pop("runner_pid", None)
                    self.lock.close()
                    return 0
                job = queued[0]
                job["status"] = "running"
                job["started"] = time.time()
                state["runner_pid"] = os.getpid()
            self.run_job(job["id"])

    def update(self, job_id, **changes):
        with jobs_state() as state:
            job = find_job(state, job_id)
            job.update(changes)
            return dict(job)

    def update_step(self, job_id, index, **changes):
        with jobs_state() as state:
            job = find_job(state, job_id)
            job["steps"][index].update(changes)
            return dict(job)

    def run_job(self, job_id):
        with jobs_state(write=False) as state:
            job = dict(find_job(state, job_id))
        log = JobLog(job["log"])
        log("== %s (requested by %s)" % (job["title"], job["by"]))
        status = "done"
        for index, step in enumerate(job["steps"]):
            current = self.update_step(job_id, index, status="running", started=time.time())
            if current.get("cancel"):
                status = "cancelled"
                self.update_step(job_id, index, status="not run")
                continue
            if status != "done":
                self.update_step(job_id, index, status="not run")
                continue
            log("\n== Step %d of %d: %s" % (index + 1, len(job["steps"]), step["title"]))
            try:
                ok = getattr(self, "step_" + step["kind"].replace("-", "_"))(
                    job_id, step, log)
            except Exception as e:  # report it in the job, don't kill the runner
                log("!! %s: %s" % (type(e).__name__, e))
                ok = False
            cancelled = self.update(job_id, pid=None).get("cancel")
            self.update_step(job_id, index, status="done" if ok else
                             "cancelled" if cancelled else "failed",
                             finished=time.time())
            if cancelled:
                status = "cancelled"
            elif not ok:
                status = "failed"
        log("\n== %s" % {"done": "Finished", "failed": "Failed",
                         "cancelled": "Cancelled"}[status])
        if job.get("recheck"):
            self.update(job_id, rechecking=True)
            log("\n== Checking what is left to update")
            try:
                with check_lock():
                    Service(load_config(), log).run(force=True, sync=False,
                                                    from_runner=True)
            except Exception as e:
                log("!! re-check failed: %s: %s" % (type(e).__name__, e))
        self.update(job_id, status=status, finished=time.time(), rechecking=False)

    # -- steps: each returns True on success --

    def step_apply_config(self, job_id, step, log):
        try:
            written = write_fixes(step["fixes"])
        except JobError as e:
            log("!! " + str(e))
            return False
        for path, line in written:
            log("  %s: %s" % (path, line))
        if not written:
            log("  (already there)")
        return True

    def step_check(self, job_id, step, log):
        plan_before = (read_json(PLAN_FILE) or {}).get("generated")
        with check_lock():
            code = Service(load_config(), log).run(force=True, sync=step.get("sync", True),
                                                   from_runner=True)
        # a check that was skipped (emerge running) leaves the old plan
        return code == 0 and (read_json(PLAN_FILE) or {}).get("generated") != plan_before

    def step_shell(self, job_id, step, log):
        env = dict(os.environ, NOCOLOR="true", TERM="dumb")
        log("$ " + step["shell"])
        proc = subprocess.Popen(["/bin/bash", "-c", step["shell"]], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        # cancel signals the whole process group
        if self.update(job_id, pid=proc.pid).get("cancel"):
            os.killpg(proc.pid, signal.SIGTERM)
        partial, latest, last = b"", None, 0.0
        while True:
            data = proc.stdout.read1(65536)
            if not data:
                break
            log.write(data)
            lines = (partial + data).split(b"\n")
            partial = lines.pop()
            for line in lines:
                latest = EMERGING.search(line) or latest
            if latest and time.monotonic() - last >= PROGRESS_EVERY:
                last = time.monotonic()
                self.update(job_id, progress={
                    "step": step["title"], "done": int(latest.group(1)) - 1,
                    "total": int(latest.group(2)),
                    "current": latest.group(3).decode(errors="replace")})
                latest = None
        code = proc.wait()
        log("(exit status %d)" % code)
        return code == 0


# -- choices -------------------------------------------------------------------

_STAGE_KEY = re.compile(r"^(remove|replace):[A-Za-z0-9+_.-]+/[A-Za-z0-9+_-]+$|^gcc-switch$")


def set_choices(text):
    """Replace choices.json, after checking every entry: excluded packages
    end up on emerge's command line, so only plain cat/pkg names pass"""
    import json
    from portage.dep import Atom
    try:
        choices = json.loads(text)
    except ValueError as e:
        raise JobError("Choices aren't valid JSON: %s" % e)
    if not isinstance(choices, dict):
        raise JobError("Choices must be a JSON object")
    clean = {}
    for key in ("exclude", "reject_fixes", "decline", "accept"):
        values = choices.get(key) or []
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise JobError("%s must be a list of strings" % key)
        for value in values:
            if key == "exclude":
                try:
                    ok = Atom(value).cp == value
                except Exception:
                    ok = False
                if not ok:
                    raise JobError("Not a package name: %s" % value)
            elif key in ("decline", "accept") and not _STAGE_KEY.match(value):
                raise JobError("Unknown choice: %s" % value)
            elif key == "reject_fixes" and ("\n" in value or len(value) > 500):
                raise JobError("Not a config line: %s" % value[:60])
        clean[key] = sorted(set(values))
    write_json(CHOICES_FILE, clean)
    return clean


# -- updater.conf ------------------------------------------------------------

def set_config(changes, path=CONFIG_FILE):
    """Change settings in updater.conf, keeping its comments"""
    clean = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise JobError("Unknown setting: %s" % key)
        if key == "interval_days":
            try:
                days = int(value)
            except ValueError:
                raise JobError("interval_days must be a number")
            if not 1 <= days <= 90:
                raise JobError("interval_days must be between 1 and 90")
            clean[key] = str(days)
        elif key == "live_exclude":
            from portage.dep import Atom
            names = value.split()
            for name in names:
                try:
                    ok = Atom(name).cp == name
                except Exception:
                    ok = False
                if not ok:
                    raise JobError("Not a package name: %s" % name)
            clean[key] = " ".join(names)
        else:
            if value.lower() not in ("yes", "no", "true", "false", "on", "off", "1", "0"):
                raise JobError("%s must be yes or no" % key)
            clean[key] = "yes" if value.lower() in ("yes", "true", "on", "1") else "no"
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = ["[updater]"]
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith(("#", ";")) and key in clean:
            lines[i] = "%s = %s" % (key, clean[key])
            done.add(key)
    lines.extend("%s = %s" % (k, v) for k, v in clean.items() if k not in done)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return load_config(path)
