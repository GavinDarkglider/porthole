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
# terminal escapes (colours, window titles, ESC E and the like), removed
# before looking for emerge's progress
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")


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
    steps.extend(shlex.join(_coloured(command)) for command in stage["commands"])
    # pick up a compiler switched by an earlier stage
    return ". /etc/profile >/dev/null 2>&1; " + " && ".join(steps)


def _coloured(command):
    """emerge turns colour off when not writing to a terminal; the log is
    shown in Porthole's terminal, which colours it, so ask for it"""
    if command and command[0] == "emerge" and not any(
            a.startswith("--color") for a in command):
        return [command[0], "--color=y"] + command[1:]
    return command


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

def later_extra(later):
    """Job fields for one that waits for the build window (later)"""
    if not later:
        return {}
    if load_config()["build_window"] is None:
        raise JobError("No build window is set (Settings, Updates).")
    return {"when": "window"}


def submit_update(generated, later=False):
    """Queue the reviewed plan; `generated` names the plan the user saw.
    later: wait for the build window"""
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

    steps = plan_steps(plan)
    count = len(plan.get("merge_list") or []) + len(
        (plan.get("preserved") or {}).get("merge_list") or [])
    lives = len((plan.get("live") or {}).get("changed") or [])
    removing = sum(1 for p in (plan.get("unneeded") or {}).get("packages") or []
                   if p["remove"])
    parts = []
    if count:
        parts.append("Update %d package%s" % (count, "s" if count != 1 else ""))
    if lives:
        parts.append("rebuild %d live" % lives if count else
                     "Rebuild %d live package%s" % (lives, "s" if lives != 1 else ""))
    if removing:
        parts.append("remove %d unneeded" % removing)
    title = " and ".join(parts)
    title = title[:1].upper() + title[1:]
    job = new_job("update", title, steps, recheck=True, plan=generated,
                  **later_extra(later))
    with jobs_state() as state:
        if any(j["kind"] == "update" for j in active_jobs(state)):
            raise JobError("An update is already queued or running.")
        state["jobs"].append(job)
        prune(state)
    return job


def plan_steps(plan):
    """Job steps carrying out a verified plan"""
    steps = []
    if plan.get("fixes"):
        steps.append({"kind": "apply-config",
                      "title": "Add the approved changes to /etc/portage",
                      "fixes": plan["fixes"]})
    for stage in plan.get("stages") or []:
        steps.append({"kind": "shell", "title": stage["title"],
                      "shell": stage_shell(stage)})
    return steps


# -- installs from Porthole -------------------------------------------------------

INSTALL_DIR = os.path.join(STATE_DIR, "installs")
_KEYWORD = re.compile(r"^(-?~?[A-Za-z0-9_-]+|\*\*|~\*|\*)$")


def check_request(text):
    """An install request from Porthole, checked: {"targets": emerge
    options and packages, "requested": [{"kind": "use"|"keywords", "line":
    ...}], "choices": {...}}"""
    import json
    from portage.dep import Atom
    try:
        request = json.loads(text)
    except ValueError as e:
        raise JobError("The request isn't valid JSON: %s" % e)
    if not isinstance(request, dict) or not isinstance(request.get("targets"), list):
        raise JobError("The request needs a list of targets")
    argv, env = check_emerge(["emerge"] + [str(t) for t in request["targets"]])
    if env:
        raise JobError("USE settings go in as package.use lines, not USE=")
    targets = [a for a in argv[1:] if a != "--color=y"]
    if any(t in ("--unmerge", "-C", "--depclean", "--resume") for t in targets) or any(
            t.startswith("-") and not t.startswith("--") and "C" in t for t in targets):
        raise JobError("Only installs are planned; unmerges run as they are")
    requested = []
    for r in request.get("requested") or []:
        kind, line = r.get("kind"), str(r.get("line", ""))
        words = line.split()
        if (kind not in ("use", "keywords", "mask") or "\n" in line
                or len(words) < (1 if kind == "mask" else 2)):
            raise JobError("Not a setting Porthole can add: %s %s" % (kind, line[:60]))
        try:
            atom = Atom(words[0])
        except Exception:
            raise JobError("Not a package atom: %s" % words[0])
        if kind == "mask":
            # "keep this version": masking what is newer, nothing else
            if len(words) != 1 or atom.operator != ">":
                raise JobError("A version lock is >cat/pkg-version: %s" % line[:60])
            requested.append({"kind": kind, "line": words[0]})
            continue
        check = _USE_FLAG if kind == "use" else _KEYWORD
        if not all(check.match(w) for w in words[1:]):
            raise JobError("Not a valid %s setting: %s" % (kind, line[:60]))
        requested.append({"kind": kind, "line": " ".join(words)})
    # planned with the --oneshot packages, then emerged after them without it
    atoms = [t for t in targets if not t.startswith("-")]
    world = [str(w) for w in request.get("world") or []]
    if world and "--oneshot" not in targets:
        raise JobError("Packages for the world file go with a --oneshot install")
    for name in world:
        if name not in atoms:
            raise JobError("Not one of the packages being installed: %s" % name[:60])
    choices = validate_choices(request.get("choices") or {})
    # True: always return the plan for review, even if nothing needs approving
    review = bool(request.get("review"))
    live_exclude = [str(n) for n in request.get("live_exclude") or []]
    for name in live_exclude:
        try:
            ok = Atom(name).cp == name
        except Exception:
            ok = False
        if not ok:
            raise JobError("Not a package name: %s" % name)
    return {"targets": targets, "requested": requested, "choices": choices,
            "world": world, "review": review, "live": bool(request.get("live")),
            "live_exclude": live_exclude, "depclean": bool(request.get("depclean")),
            # a world update the user approved: its choices become the
            # standing ones (for the weekly checks)
            "save_choices": bool(request.get("save_choices")) and "@world" in targets}


def store_install_plan(plan):
    """Keep a root-made install plan (the last few) where users can read it"""
    os.makedirs(INSTALL_DIR, mode=0o755, exist_ok=True)
    name = "install-%s-%s.json" % (time.strftime("%Y%m%d-%H%M%S"), os.urandom(2).hex())
    path = os.path.join(INSTALL_DIR, name)
    write_json(path, plan)
    for old in sorted(os.listdir(INSTALL_DIR))[:-20]:
        try:
            os.unlink(os.path.join(INSTALL_DIR, old))
        except OSError:
            pass
    return path


def submit_install(text, expect=None, log=None, later=False):
    """With expect (the fingerprint of the plan the user reviewed, made as
    the user): queue a job that plans again as root and installs only if
    it matches. Without: plan as root now, queuing it if it needs no
    approval. Returns ("queued", job) or ("review", plan_path)."""
    from porthole.updater.analyze import Analyzer
    request = check_request(text)
    if expect:
        if not re.match(r"^[0-9a-f]{24}$", expect):
            raise JobError("Not a plan fingerprint: %s" % expect[:40])
        wanted = [t for t in request["targets"] if not t.startswith("-")]
        world = "@world" in wanted
        title = "Update" if world else (
            "Install " + ", ".join(wanted[:3])
            + (" and %d more" % (len(wanted) - 3) if len(wanted) > 3 else ""))
        job = new_job("update" if world else "install", title,
                      [{"kind": "plan-install", "title": "Check the plan you approved",
                        "request": request, "expect": expect}], recheck=True,
                      **later_extra(later))
        with jobs_state() as state:
            if world and any(j["kind"] == "update" for j in active_jobs(state)):
                raise JobError("An update is already queued or running.")
            state["jobs"].append(job)
            prune(state)
        return "queued", job
    plan = Analyzer(request["targets"], choices=request["choices"],
                    requested=request["requested"], world=request["world"],
                    log=log or (lambda m: None)).analyze()
    plan["request"] = request
    path = store_install_plan(plan)
    if plan.get("status") == "ready" and not request["review"]:
        return "queued", queue_install(plan, path)
    return "review", path


def submit_install_plan(path, generated, later=False):
    """Queue an install plan the user reviewed"""
    path = os.path.realpath(path)
    if os.path.dirname(path) != os.path.realpath(INSTALL_DIR):
        raise JobError("Not an install plan: %s" % path)
    plan = read_json(path)
    if not plan or plan.get("generated") != generated:
        raise JobError("The plan changed after you reviewed it. Review it again.")
    if plan.get("status") not in ("ready", "needs-approval"):
        raise JobError("This plan can't be run automatically (%s)." % plan.get("status"))
    stale = stale_reason(plan)
    if stale:
        raise JobError(stale + " Work out the install again.")
    return queue_install(plan, path, later)


def queue_install(plan, path, later=False):
    wanted = [t for t in plan["request"]["targets"] if not t.startswith("-")]
    job = new_job("install", "Install " + ", ".join(wanted[:3])
                  + (" and %d more" % (len(wanted) - 3) if len(wanted) > 3 else ""),
                  plan_steps(plan), recheck=True, plan=plan["generated"], plan_file=path,
                  **later_extra(later))
    with jobs_state() as state:
        state["jobs"].append(job)
        prune(state)
    return job


def submit_check(sync=True):
    """Queue an update check (syncing first, at most once a day; with
    sync="now", whenever asked)"""
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


def cancel(job_id=None, keep_rest=True):
    """Cancel queued jobs and stop the running one (or just job_id).
    keep_rest False: the user doesn't want what an install didn't get to
    back in their install queue (see resume.py)"""
    stopped = []
    with jobs_state() as state:
        for job in active_jobs(state):
            if job_id and job["id"] != job_id:
                continue
            job["keep_rest"] = keep_rest
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
                        os.killpg(pid, signal.SIGCONT)  # if paused
                    except OSError:
                        pass
            stopped.append(job["id"])
    return stopped


def preserved_rebuild_packages(log=lambda m: None):
    """What 'emerge @preserved-rebuild' would rebuild now (package names)"""
    try:
        import portage
        from portage._sets import load_default_config
        portage.db[portage.root]["vartree"].dbapi._plib_registry.load()
        config = load_default_config(portage.settings, portage.db[portage.root])
        return sorted({atom.cp for atom in config.getSetAtoms("preserved-rebuild")})
    except Exception as e:
        log("!! couldn't check for old libraries: %s" % e)
        return []


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

class _PtyReader:
    """read1() over a pty master; EOF once the child side has closed"""

    def __init__(self, fd):
        self.fd = fd

    def read1(self, size):
        try:
            return os.read(self.fd, size)
        except OSError:  # EIO: every writer has gone
            os.close(self.fd)
            return b""


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
        self.window_job = False

    def run(self):
        os.makedirs(os.path.dirname(RUNNER_LOCK), exist_ok=True)
        self.lock = open(RUNNER_LOCK, "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # the running one will get to the queue
        os.makedirs(JOB_LOG_DIR, mode=0o755, exist_ok=True)
        from porthole.updater.window import can_start, in_window
        while True:
            config = load_config()
            allowed, why = can_start(config)
            with jobs_state() as state:
                queued = [j for j in state["jobs"] if j["status"] == "queued"]
                # what the user started runs now; the rest waits for the window
                ready = [j for j in queued if j.get("when") != "window" or allowed]
                waiting = [j for j in queued if j not in ready]
                for j in waiting:
                    j["waiting"] = why
                if not ready and not (waiting and in_window(config["build_window"])):
                    # let go while the list is still locked, so a job
                    # submitted now finds the lock free for its own runner.
                    # Outside the window cron starts a runner again.
                    state.pop("runner_pid", None)
                    state.pop("runner_waiting", None)
                    self.lock.close()
                    return 0
                state["runner_pid"] = os.getpid()
                if not ready:
                    # in the window, but on battery or someone is working
                    state["runner_waiting"] = why
                    job = None
                else:
                    state.pop("runner_waiting", None)
                    job = ready[0]
                    job.pop("waiting", None)
                    job["status"] = "running"
                    job["started"] = time.time()
            if job is None:
                time.sleep(60)
                continue
            self.window_job = job.get("when") == "window"
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
        index = -1
        while True:
            # steps can add steps (plan-install), so read them each time
            with jobs_state(write=False) as state:
                steps = find_job(state, job_id)["steps"]
            index += 1
            if index >= len(steps):
                break
            step = steps[index]
            current = self.update_step(job_id, index, status="running", started=time.time())
            if current.get("cancel"):
                status = "cancelled"
                self.update_step(job_id, index, status="not run")
                continue
            if status != "done":
                self.update_step(job_id, index, status="not run")
                continue
            log("\n== Step %d: %s" % (index + 1, step["title"]))
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
        # the job is over now: the job window stops following it here, and
        # the re-check below shows in the tray as a check. One that stopped
        # part way may leave programs using old libraries: only root can
        # tell, so it is noted for the job window to offer a rebuild
        extra = {}
        if status != "done":
            extra["preserved_rebuild"] = preserved_rebuild_packages(log)
        self.update(job_id, status=status, finished=time.time(), **extra)
        if job.get("recheck"):
            log("\n== Checking what is left to update")
            try:
                with check_lock():
                    Service(load_config(), Log(echo=False)).run(
                        force=True, sync=False, from_runner=True)
            except Exception as e:
                log("!! re-check failed: %s: %s" % (type(e).__name__, e))

    # -- steps: each returns True on success --

    def step_plan_install(self, job_id, step, log):
        """Plan an install as root and carry it out only if it is the plan
        the user approved (same fingerprint): then its steps are added"""
        from porthole.updater.analyze import Analyzer, plan_signature
        request = step["request"]
        log("Working out the install as root, to compare with the plan you approved...")
        plan = Analyzer(request["targets"], choices=request["choices"],
                        requested=request["requested"], log=log,
                        world=request.get("world") or (),
                        live=request.get("live", False),
                        live_exclude=request.get("live_exclude") or (),
                        depclean=request.get("depclean", False)).analyze()
        plan["request"] = request
        path = store_install_plan(plan)
        if plan.get("status") not in ("ready", "needs-approval"):
            log("!! It can't go ahead as it stands (%s); see %s" % (plan.get("status"), path))
            return False
        signature = plan_signature(plan)
        if signature != step["expect"]:
            again = ("Check for updates again" if "@world" in request["targets"]
                     else "Process the install queue again")
            log("!! The plan differs from the one you approved (something changed since): "
                "nothing was done. %s to review it. (New plan: %s)" % (again, path))
            return False
        log("Same plan as approved.")
        if request.get("save_choices"):
            write_json(CHOICES_FILE, request["choices"])
            log("Your choices are kept for later checks.")
        with jobs_state() as state:
            job = find_job(state, job_id)
            here = next(i for i, s in enumerate(job["steps"])
                        if s["kind"] == "plan-install" and s.get("expect") == step["expect"])
            job["steps"][here + 1:here + 1] = [
                dict(s, status="queued") for s in plan_steps(plan)]
            job["plan_file"] = path
        return True

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
        log("$ " + step["shell"])
        return self.run_logged(job_id, step, log, ["/bin/bash", "-c", step["shell"]])

    def step_argv(self, job_id, step, log):
        """A checked emerge command line, run without a shell"""
        log("$ " + " ".join("%s=%s" % kv for kv in sorted(step.get("env", {}).items()))
            + (" " if step.get("env") else "") + shlex.join(step["argv"]))
        return self.run_logged(job_id, step, log, step["argv"], step.get("env"))

    def run_logged(self, job_id, step, log, argv, extra_env=None):
        """Run argv into the job log, noting emerge's progress. It gets a
        pseudo-terminal, so emerge behaves as in a terminal: colours, and
        the window titles Porthole's terminal shows"""
        import pty
        import select
        from porthole.updater.window import ACTIVE, idle_for, low_priority_env
        config = load_config()
        env = dict(os.environ, TERM="xterm", **(extra_env or {}))
        if config["low_priority"] or self.window_job:
            env.update(low_priority_env())
        env.pop("NOCOLOR", None)
        master, slave = pty.openpty()
        try:
            import fcntl as _fcntl, struct, termios
            _fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        except OSError:
            pass
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=slave,
                                stderr=slave, start_new_session=True)
        os.close(slave)
        proc.stdout = _PtyReader(master)
        # cancel signals the whole process group
        if self.update(job_id, pid=proc.pid).get("cancel"):
            os.killpg(proc.pid, signal.SIGTERM)
        partial, latest, last = b"", None, 0.0
        pausing = self.window_job and config["pause_when_active"]
        paused, checked = False, 0.0
        while True:
            if pausing and time.monotonic() - checked >= 15:
                checked = time.monotonic()
                idle = idle_for()
                resume_after = max(config["idle_minutes"] * 60, 5 * 60)
                if not paused and idle is not None and idle < ACTIVE:
                    os.killpg(proc.pid, signal.SIGSTOP)
                    paused = True
                    self.update(job_id, paused=time.time())
                    log("\n== Paused: someone is using the computer. It carries on "
                        "once they have been away %d minutes." % (resume_after // 60))
                elif paused and (idle is None or idle >= resume_after):
                    os.killpg(proc.pid, signal.SIGCONT)
                    paused = False
                    self.update(job_id, paused=None)
                    log("== Carrying on.\n")
            if pausing and not select.select([master], [], [], 5)[0]:
                if proc.poll() is None:
                    continue
            data = proc.stdout.read1(65536)
            if not data:
                break
            log.write(data)
            lines = (partial + data).split(b"\n")
            partial = lines.pop()
            for line in lines:
                latest = EMERGING.search(ANSI.sub(b"", line)) or latest
            if latest and time.monotonic() - last >= PROGRESS_EVERY:
                last = time.monotonic()
                self.update(job_id, progress={
                    "step": step["title"], "done": int(latest.group(1)) - 1,
                    "total": int(latest.group(2)),
                    "current": latest.group(3).decode(errors="replace")})
                latest = None
        if paused:
            self.update(job_id, paused=None)
        code = proc.wait()
        log("(exit status %d)" % code)
        return code == 0


# -- emerge jobs from Porthole ---------------------------------------------------

# emerge options Porthole's actions and its advanced emerge dialog use; a
# job may only contain these, package atoms and sets (nothing that changes
# where or what emerge works on, and nothing interactive)
EMERGE_FLAGS = {
    "--alphabetical", "--buildpkg", "--buildpkgonly", "--changed-use", "--columns",
    "--debug", "--deep", "--depclean", "--emptytree", "--fetch-all-uri", "--fetchonly",
    "--getbinpkg", "--getbinpkgonly", "--ignore-default-opts", "--keep-going",
    "--newuse", "--noconfmem", "--nodeps", "--noreplace", "--nospinner", "--oneshot",
    "--onlydeps", "--quiet", "--resume", "--skipfirst", "--tree", "--unmerge",
    "--update", "--usepkg", "--usepkgonly", "--verbose",
}
EMERGE_VALUES = {
    "--with-bdeps": re.compile(r"^[yn]$"),
    "--color": re.compile(r"^[yn]$"),
    "--jobs": re.compile(r"^\d{1,3}$"),
    "--load-average": re.compile(r"^\d{1,3}(\.\d+)?$"),
    "--exclude": None,  # an atom, checked as one
}
EMERGE_SHORT = set("uDN1vqfkKgGeOtC")
_USE_FLAG = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9+_@.-]*$")
_SET = re.compile(r"^@[A-Za-z0-9_-]+$")


def check_emerge(tokens):
    """(argv, env) for an emerge command line from Porthole, e.g.
    ['USE=gtk -qt', 'emerge', '--oneshot', '=cat/pkg-1']; JobError if any
    part isn't allowed"""
    from portage.dep import Atom
    env, i = {}, 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-"):
        key, _sep, value = tokens[i].partition("=")
        if key != "USE" or not all(_USE_FLAG.match(f) for f in value.split()):
            raise JobError("Only a USE setting may come before emerge: %s" % tokens[i])
        env["USE"] = value
        i += 1
    if i >= len(tokens) or tokens[i] != "emerge":
        raise JobError("Only emerge commands can be queued")
    argv, atoms = ["emerge"], []
    for token in tokens[i + 1:]:
        if token == "--ask" or token == "-a":
            continue  # nobody to answer; the user confirmed in Porthole
        if token in EMERGE_FLAGS:
            argv.append(token)
        elif token.startswith("--") and "=" in token:
            name, _sep, value = token.partition("=")
            if name not in EMERGE_VALUES:
                raise JobError("Not an option Porthole can queue: %s" % token)
            check = EMERGE_VALUES[name]
            if check is None:
                try:
                    Atom(value, allow_wildcard=True)
                except Exception:
                    raise JobError("Not a package atom: %s" % value)
            elif not check.match(value):
                raise JobError("Bad value: %s" % token)
            argv.append(token)
        elif token.startswith("--"):
            raise JobError("Not an option Porthole can queue: %s" % token)
        elif token.startswith("-"):
            if not token[1:] or not set(token[1:]) <= EMERGE_SHORT:
                raise JobError("Not an option Porthole can queue: %s" % token)
            argv.append(token)
        elif _SET.match(token):
            atoms.append(token)
        else:
            try:
                Atom(token)
            except Exception:
                raise JobError("Not a package atom: %s" % token)
            atoms.append(token)
    if not atoms and "--resume" not in argv:
        raise JobError("No packages given")
    return _coloured(argv + atoms), env


def submit_emerge(tokens):
    """Queue an emerge or unmerge from Porthole's package actions"""
    argv, env = check_emerge(tokens)
    atoms = [a for a in argv[1:] if not a.startswith("-")]
    verb = "Unmerge" if "--unmerge" in argv or "-C" in argv else "Emerge"
    title = "%s %s" % (verb, ", ".join(atoms[:3]) + (" and %d more" % (len(atoms) - 3)
                                                     if len(atoms) > 3 else ""))
    job = new_job("emerge", title or verb,
                  [{"kind": "argv", "title": title, "argv": argv, "env": env}],
                  recheck=True)
    with jobs_state() as state:
        state["jobs"].append(job)
        prune(state)
    return job


def submit_command(line):
    """Queue a custom command line from an unlocked Porthole, which showed
    it to the user to confirm. It runs as root in bash, without input."""
    line = line.strip()
    if not line or "\n" in line or "\0" in line:
        raise JobError("A custom command is one line")
    title = "Run: " + (line if len(line) <= 60 else line[:57] + "...")
    job = new_job("command", title, [{"kind": "shell", "title": title, "shell": line}],
                  recheck=True)
    with jobs_state() as state:
        state["jobs"].append(job)
        prune(state)
    return job


# -- choices -------------------------------------------------------------------

_STAGE_KEY = re.compile(r"^(remove|replace|depclean):[A-Za-z0-9+_.-]+/[A-Za-z0-9+_-]+$|^gcc-switch$")


def set_choices(text):
    """Replace choices.json, after checking every entry"""
    import json
    try:
        choices = json.loads(text)
    except ValueError as e:
        raise JobError("Choices aren't valid JSON: %s" % e)
    clean = validate_choices(choices)
    write_json(CHOICES_FILE, clean)
    return clean


def validate_choices(choices):
    """Checked choices: excluded packages end up on emerge's command line,
    so only plain cat/pkg names pass"""
    from portage.dep import Atom
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
        elif key == "build_window":
            from porthole.updater.window import format_window, parse_window
            try:
                window = parse_window(value)
            except ValueError as e:
                raise JobError(str(e))
            clean[key] = format_window(window) if window else ""
        elif key == "idle_minutes":
            try:
                minutes = int(value)
            except ValueError:
                raise JobError("idle_minutes must be a number")
            if not 0 <= minutes <= 240:
                raise JobError("idle_minutes must be between 0 and 240")
            clean[key] = str(minutes)
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
