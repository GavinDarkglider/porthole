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

from porthole.updater import targets
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
# a step that failed but added steps to fix it and run it again returns this
AGAIN = "again"
# how many times one step may be run again that way
MAX_ATTEMPTS = 6
# what a step keeps of itself when it is run again (the rest is from its run)
_RUN_KEYS = ("status", "started", "finished", "cancel")
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
    portage_dir = targets.of_plan(plan).portage_dir
    if (last_repo_sync() or 0) > checked + GRACE:
        return "The repositories were synced after this check."
    if newest_config_change(portage_dir) > checked + GRACE:
        return "%s was changed after this check." % portage_dir
    return None


def write_fixes(fixes, portage_dir=PORTAGE_DIR):
    """Append approved lines to <portage_dir>/package.*/zz-porthole"""
    for fix in fixes:
        path = os.path.join(portage_dir, CONFIG_DIRS[fix["kind"]])
        if os.path.isfile(path):
            raise JobError(
                "%s is a single file. Porthole keeps its changes in its own file, "
                "so it needs %s to be a directory. Move the file into a new "
                "directory of that name and re-check." % (path, path))
    stamp = time.strftime("%Y-%m-%d")
    written = []
    for fix in fixes:
        directory = os.path.join(portage_dir, CONFIG_DIRS[fix["kind"]])
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


def stage_shell(stage, portage_dir=PORTAGE_DIR):
    """One shell command line for a stage; stops at the first failure"""
    temp = os.path.join(portage_dir, CONFIG_DIRS["use"], TEMP_NAME)
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
    if "helpers" in extra:
        from porthole.updater.ondemand import check_helpers
        try:
            extra["helpers"] = check_helpers(extra["helpers"])
        except ValueError as e:
            raise JobError(str(e))
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


def submit_update(generated, later=False, auto=None):
    """Queue the reviewed plan; `generated` names the plan the user saw.
    later: wait for the build window. auto: queued by the service itself
    (automation = safe), with the package versions it updates"""
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
    extra = later_extra(later)
    if auto is not None:
        decided = (plan.get("status") == "needs-approval"
                   or (plan.get("auto") or {}).get("refused"))
        extra.update(auto=True, cpvs=auto, by="automatic (%s)" % (
            "Porthole decided; see News" if decided else "nothing to decide"))
    job = new_job("update", title, steps, recheck=True, plan=generated, **extra)
    with jobs_state() as state:
        if any(j["kind"] == "update" for j in active_jobs(state)):
            raise JobError("An update is already queued or running.")
        state["jobs"].append(job)
        prune(state)
    return job


def plan_steps(plan):
    """Job steps carrying out a verified plan"""
    steps = []
    target = targets.of_plan(plan)
    if plan.get("fixes"):
        steps.append({"kind": "apply-config",
                      "title": "Add the approved changes to " + target.portage_dir,
                      "fixes": plan["fixes"], "target": target.name})
    for stage in plan.get("stages") or []:
        steps.append({"kind": "shell", "title": stage["title"],
                      "shell": stage_shell(stage, target.portage_dir)})
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
        return "queued", queue_install(plan, path, later)
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


def keep_building(job_id=None):
    """The user wants the build to go on although they're at the computer:
    carry on now and don't pause this job again. Returns the job id."""
    with jobs_state() as state:
        job = (find_job(state, job_id) if job_id else
               next((j for j in state["jobs"] if j["status"] == "running"), None))
        if not job or job["status"] not in ACTIVE:
            raise JobError("No running or queued job to keep building")
        job["keep_building"] = True
        if job.get("paused") and job.get("pid"):
            try:
                os.killpg(job["pid"], signal.SIGCONT)
            except OSError:
                pass
        job["paused"] = None
        return job["id"]


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
        # the step running now (add_steps puts steps after it)
        self.step_index = None

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
        # services the job needs only while it runs (ondemand.py): stopped
        # after its last step whatever happens
        from porthole.updater.ondemand import Helpers
        helpers = Helpers(job_id, [])
        self.job_env = {}
        try:
            helpers = Helpers(job_id, job.get("helpers") or [])
            self.job_env = helpers.start(log)
        except Exception as e:
            log("!! %s" % e)
            status = "failed"
        try:
            status = self.run_steps(job_id, log, status)
        finally:
            helpers.stop(log)
            self.job_env = {}
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
        with jobs_state(write=False) as state:
            news_item = find_job(state, job_id).get("news")  # added after queuing
        if news_item:
            # the automatic update's news item gets its outcome
            try:
                from porthole.updater import autopilot, news
                news.append_porthole_item(news_item, autopilot.result_text(
                    dict(job, status=status)))
            except Exception as e:
                log("!! couldn't add the result to the news item: %s" % e)
        if job.get("recheck"):
            log("\n== Checking what is left to update")
            try:
                with check_lock():
                    Service(load_config(), Log(echo=False)).run(
                        force=True, sync=False, from_runner=True)
            except Exception as e:
                log("!! re-check failed: %s: %s" % (type(e).__name__, e))

    def run_steps(self, job_id, log, status):
        """Run a job's steps in turn; once one fails (or status isn't
        "done" to begin with) the rest are not run. Returns the status."""
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
            self.step_index = index
            try:
                result = getattr(self, "step_" + step["kind"].replace("-", "_"))(
                    job_id, step, log)
            except Exception as e:  # report it in the job, don't kill the runner
                log("!! %s: %s" % (type(e).__name__, e))
                result = False
            cancelled = self.update(job_id, pid=None).get("cancel")
            # AGAIN: it failed, but added what fixes it and itself again
            again = result == AGAIN and not cancelled
            self.update_step(job_id, index, status="cancelled" if cancelled else
                             "fixed" if again else "done" if result else "failed",
                             finished=time.time())
            if cancelled:
                status = "cancelled"
            elif not result:
                status = "failed"
        return status

    def add_steps(self, job_id, steps, again=None, why=None):
        """Put steps into the running job right after the step running now,
        and (again: a dict, maybe empty, of what changes in it) that step
        once more after them. For a step that failed in a way it can fix:
        it adds the fix and itself, then returns AGAIN. False when the step
        has been run again too often already (nothing is added then)."""
        with jobs_state() as state:
            job = find_job(state, job_id)
            index = self.step_index
            current = job["steps"][index]
            new = [dict(s, status="queued", added=why or True) for s in steps]
            if again is not None:
                attempt = current.get("attempt", 1)
                if attempt >= MAX_ATTEMPTS:
                    return False
                title = current.get("first_title", current["title"])
                copy = {k: v for k, v in current.items() if k not in _RUN_KEYS}
                copy.update(again, status="queued", attempt=attempt + 1, first_title=title,
                            title="%s (again)" % title, added=why or True)
                new.append(copy)
            job["steps"][index + 1:index + 1] = new
        return True

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
            # only a target root knows of (targets.conf), never a path from the job
            written = write_fixes(step["fixes"], targets.get(step.get("target")).portage_dir)
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

    def step_plugin(self, job_id, step, log):
        """A step a plugin provides (its root.py STEPS), if the plugin is
        still switched on"""
        from porthole import plugins
        try:
            module = plugins.root_module(step.get("plugin"))
        except plugins.PluginError as e:
            log("!! %s" % e)
            return False
        action = (getattr(module, "STEPS", {}) or {}).get(step.get("action"))
        if action is None:
            log("!! The %s plugin has no step %s" % (step.get("plugin"), step.get("action")))
            return False
        result = action(self, job_id, step, log)
        return AGAIN if result == AGAIN else bool(result)

    def step_repo_name(self, job_id, step, log):
        """After a sync: if the repository calls itself something else,
        use its own name (portage ignores it otherwise)"""
        from porthole.updater import repopool
        name = step.get("name", "")
        location = roots_repos().get(name)
        own = repo_own_name(location) if location else None
        if not own or own == name:
            log("%s: name ok" % name)
            return True
        log("%s calls itself %s (profiles/repo_name); portage only uses it under that "
            "name. Renaming it:" % (name, own))
        try:
            for path in rename_repo(name, own):
                log("   " + path)
        except JobError as e:
            log("!! %s" % e)
            return False
        return True

    def step_shell(self, job_id, step, log):
        log("$ " + step["shell"])
        return self.run_logged(job_id, step, log, ["/bin/bash", "-c", step["shell"]])

    def step_argv(self, job_id, step, log):
        """A checked emerge command line, run without a shell"""
        log("$ " + " ".join("%s=%s" % (k, "(Porthole's list)" if "\n" in v else v)
                            for k, v in sorted(step.get("env", {}).items()))
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
        env = dict(os.environ, TERM="xterm", **getattr(self, "job_env", {}))
        env.update(extra_env or {})
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
                with jobs_state(write=False) as state:
                    wanted = find_job(state, job_id).get("keep_building")
                if wanted:
                    # the user said to keep going (they may be watching it)
                    pausing = False
                    if paused:
                        paused = False
                        log("== Carrying on: you asked to keep building.\n")
                    continue
                idle = idle_for()
                # the admin's own "nobody has used it for" setting, at least a minute
                resume_after = max(config["idle_minutes"] * 60, 60)
                if not paused and idle is not None and idle < ACTIVE:
                    os.killpg(proc.pid, signal.SIGSTOP)
                    paused = True
                    self.update(job_id, paused=time.time())
                    log("\n== Paused: someone is using the computer. It carries on "
                        "once nobody has used it for %d minute%s, or when you choose "
                        "Keep building." % (resume_after // 60,
                                             "" if resume_after < 120 else "s"))
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


# -- licence policy ------------------------------------------------------------

# which licences are fine, as Settings > System offers them: written as
# "*/* ..." in a package.license file of Porthole's own, read first, so
# per-package exceptions (the user's, or approved fixes) still apply.
# "system": no file, ACCEPT_LICENSE from make.conf and the profile decides
LICENCE_POLICIES = {
    "free": "-* @FREE",
    "redistributable": "-* @FREE @BINARY-REDISTRIBUTABLE",
    "eula": "-* @FREE @BINARY-REDISTRIBUTABLE @EULA",
    "all": "*",
    "system": None,
}
LICENCE_FILE = "00-porthole-licences"


def licence_policy_path(target_name=None):
    return os.path.join(targets.get(target_name).portage_dir,
                        CONFIG_DIRS["licenses"], LICENCE_FILE)


def read_licence_policy(target_name=None):
    """The policy Porthole set for a target, or "system" (none)"""
    try:
        with open(licence_policy_path(target_name)) as f:
            lines = [l.split(None, 1) for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return "system"
    groups = " ".join(l[1].strip() for l in lines if l[0] == "*/*" and len(l) > 1)
    return next((k for k, v in LICENCE_POLICIES.items() if v == groups), "custom")


def set_licence_policy(policy, target_name=None):
    """Write (or with "system", remove) the licence policy of a target"""
    if policy not in LICENCE_POLICIES:
        raise JobError("Not a licence policy: %s (one of %s)"
                       % (policy, ", ".join(LICENCE_POLICIES)))
    try:
        path = licence_policy_path(target_name)
    except KeyError:
        raise JobError("No such system: %s" % target_name)
    directory = os.path.dirname(path)
    if os.path.isfile(directory):
        raise JobError(
            "%s is a single file. Porthole keeps its policy in its own file, so it "
            "needs %s to be a directory. Move the file into a new directory of that "
            "name and try again." % (directory, directory))
    if LICENCE_POLICIES[policy] is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return policy
    os.makedirs(directory, exist_ok=True)
    with open(path + ".new", "w") as f:
        f.write("# Which licences are fine for this system: set in Porthole, Settings,\n"
                "# System. Porthole replaces this file; per-package exceptions belong in\n"
                "# other files of this directory (they still apply).\n"
                "*/* %s\n" % LICENCE_POLICIES[policy])
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return policy


# MAKEOPTS for emerge run by hand on this computer, set by Porthole: an
# env file applied to every package through package.env ("*/*"), read
# first so the user's per-package env files still win. make.conf isn't
# touched. Porthole's own jobs set MAKEOPTS in their environment, which
# wins over package.env (the build cluster's value, see ondemand.py).
MAKEOPTS_NAME = "00-porthole-makeopts"


def makeopts_paths(portage_dir="/etc/portage"):
    """(env file, package.env file) of Porthole's MAKEOPTS"""
    return (os.path.join(portage_dir, "env", MAKEOPTS_NAME + ".conf"),
            os.path.join(portage_dir, "package.env", MAKEOPTS_NAME))


def local_makeopts():
    """The safe MAKEOPTS for this computer: one job per thread, no more than
    one per 2 GB of memory; no new jobs while the load is above the threads"""
    from porthole.updater.ondemand import safe_jobs
    return "-j%d -l%d" % (safe_jobs(), os.cpu_count() or 1)


def read_makeopts(portage_dir="/etc/portage"):
    """The MAKEOPTS Porthole set, or None (make.conf decides)"""
    env, pkgenv = makeopts_paths(portage_dir)
    if not os.path.isfile(pkgenv):
        return None
    try:
        with open(env) as f:
            for line in f:
                match = re.match(r'^MAKEOPTS="([^"]*)"', line.strip())
                if match:
                    return match.group(1)
    except OSError:
        pass
    return None


def set_makeopts(mode, portage_dir="/etc/portage"):
    """mode "auto": Porthole sets MAKEOPTS for this computer (worked out
    again by the scheduled check if the computer changes); "off": make.conf
    decides again"""
    env, pkgenv = makeopts_paths(portage_dir)
    if mode == "off":
        for path in (pkgenv, env):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return "make.conf decides MAKEOPTS"
    if mode != "auto":
        raise JobError("makeopts is auto or off")
    for directory in (os.path.dirname(env), os.path.dirname(pkgenv)):
        if os.path.isfile(directory):
            raise JobError(
                "%s is a single file. Porthole keeps its settings in its own file, so it "
                "needs %s to be a directory. Move the file into a new directory of that "
                "name and try again." % (directory, directory))
    value = local_makeopts()
    for path, text in (
            (env, "# Porthole: MAKEOPTS for emerge run by hand, worked out from this\n"
                  "# computer's CPU threads and memory (Settings, System, Build options).\n"
                  "# Porthole replaces this file; its own builds set MAKEOPTS for each build.\n"
                  'MAKEOPTS="%s"\n' % value),
            (pkgenv, "# Porthole: its MAKEOPTS for every package (Settings, System, Build\n"
                     "# options). Per-package env files of your own still win.\n"
                     "*/* %s.conf\n" % MAKEOPTS_NAME)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w") as f:
            f.write(text)
        os.chmod(path + ".new", 0o644)
        os.replace(path + ".new", path)
    return value


def refresh_makeopts(portage_dir="/etc/portage"):
    """Work Porthole's MAKEOPTS out again (more memory, another CPU), if
    Porthole sets it; the new value, or None"""
    current = read_makeopts(portage_dir)
    if current is not None and current != local_makeopts():
        return set_makeopts("auto", portage_dir)
    return None


def set_global_use(text, target_name=None):
    """Write Porthole's global USE file of a target from a JSON model
    (system.check_global_use), or remove it when there's nothing to set"""
    import json
    from porthole.updater import system
    try:
        model = system.check_global_use(json.loads(text))
        content = system.render_global_use(model)
    except ValueError as e:
        raise JobError(str(e))
    try:
        portage_dir = targets.get(target_name).portage_dir
    except KeyError:
        raise JobError("No such system: %s" % target_name)
    path = system.global_use_path(portage_dir)
    directory = os.path.dirname(path)
    if os.path.isfile(directory):
        raise JobError(
            "%s is a single file. Porthole keeps its changes in its own file, so it "
            "needs %s to be a directory. Move the file into a new directory of that "
            "name and try again." % (directory, directory))
    if content is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return "removed"
    os.makedirs(directory, exist_ok=True)
    with open(path + ".new", "w") as f:
        f.write(content)
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return "written"


def set_profile(name, target_name=None):
    """Switch a target's profile (only to one its repositories list)"""
    from porthole.updater import system
    try:
        target = targets.get(target_name)
    except KeyError:
        raise JobError("No such system: %s" % target_name)
    match = [p for p in system.profiles() if p[0] == name]
    if not match:
        raise JobError("Not a profile for this computer: %s" % name)
    try:
        system.link_profile(target.portage_dir, match[0][2], name)
    except (ValueError, OSError) as e:
        raise JobError(str(e))
    return name


# -- repositories ---------------------------------------------------------------

REPOS_DIR = "/var/db/repos"


# the repositories for other systems only (repopool.py), as a target name
SYSTEMS = "systems"


def roots_repos():
    """{name: location} a system may use: this computer's repositories and
    the list for other systems"""
    from porthole.updater import repopool, system
    import configparser
    found = {}
    for name, path in system.repos_conf_sections().items():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path)
            location = parser.get(name, "location", fallback="").strip()
        except configparser.Error:
            continue
        if location:
            found[name] = location.rstrip("/") or "/"
    if "gentoo" not in found:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read("/usr/share/portage/config/repos.conf")
            found["gentoo"] = parser.get("gentoo", "location").strip()
        except configparser.Error:
            found["gentoo"] = "/var/db/repos/gentoo"
    for name, repo in repopool.repos().items():
        found.setdefault(name, repo["location"])
    return found


def submit_repo_sync(name):
    """Queue a sync of one repository (re-checks the plan afterwards). One
    for other systems only is synced with emaint given only Porthole's list."""
    from porthole.updater import repopool, system
    if not system._REPO_NAME.match(name or ""):
        raise JobError("Not a repository name: %s" % name)
    # portage won't even find a repository listed under another name than
    # its own, so a checkout that is there already gets its name first
    name = own_name_first(name)
    step = {"kind": "argv", "title": "Sync %s" % name,
            "argv": ["emaint", "sync", "--repo", name]}
    if name in repopool.repos():
        step["env"] = {"PORTAGE_REPOSITORIES": repopool.environment([name])}
    job = new_job("sync", "Sync %s" % name,
                  [step, {"kind": "repo-name", "title": "Check its name", "name": name}],
                  recheck=True)
    with jobs_state() as state:
        state["jobs"].append(job)
        prune(state)
    return job


def repo_own_name(location):
    """The name a repository gives itself (profiles/repo_name), or None"""
    try:
        with open(os.path.join(location, "profiles", "repo_name")) as f:
            name = f.read().strip()
    except OSError:
        return None
    from porthole.updater import system
    return name if system._REPO_NAME.match(name) else None


def own_name_first(name):
    """The repository's name after calling it by its own (if its checkout
    is there and says otherwise); the name as it was if that can't be done"""
    location = roots_repos().get(name)
    own = repo_own_name(location) if location else None
    if own and own != name:
        try:
            rename_repo(name, own)
            return own
        except JobError:
            pass
    return name


def rename_repo(old, new, portage_dir="/etc/portage"):
    """Call a repository by the name it gives itself, everywhere Porthole
    wrote it: this computer's repos.conf/<old>.conf (a file of its own),
    Porthole's list, and the systems' repos.conf. portage ignores an
    entry whose name differs from the repository's own. The checkout stays
    where it is. Returns what changed."""
    import configparser
    from porthole.updater import repopool, system
    if not system._REPO_NAME.match(new or "") or old == new:
        raise JobError("Not a new name: %s" % new)
    if new in all_repo_names():
        raise JobError("There already is a repository called %s" % new)
    changed = []
    sections = system.repos_conf_sections(portage_dir)
    if old in sections:
        path = sections[old]
        if [n for n, f in sections.items() if f == path and n != old]:
            raise JobError("%s defines other repositories too; rename [%s] there by hand"
                           % (path, old))
        with open(path) as f:
            text = f.read()
        text = re.sub(r"^\[%s\]" % re.escape(old), "[%s]" % new, text, flags=re.M)
        directory = os.path.dirname(path)
        target_path = os.path.join(directory, new + ".conf") \
            if os.path.basename(path) == old + ".conf" else path
        with open(target_path + ".new", "w") as f:
            f.write(text)
        os.chmod(target_path + ".new", 0o644)
        os.replace(target_path + ".new", target_path)
        if target_path != path:
            os.unlink(path)
        changed.append(target_path)
    pool = repopool.repos()
    if old in pool:
        repo = pool[old]
        repopool.remove(old)
        repopool.add(new, repo["location"], repo["sync_type"], repo["sync_uri"],
                     repo["auto_sync"])
        changed.append(repopool.POOL_FILE)
    for target in targets.load().values():
        if target.local:
            continue
        names = repopool.target_repos(target.portage_dir)
        if old in names:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(repopool.target_file(target.portage_dir))
            locations = {(new if n == old else n): parser.get(n, "location")
                         for n in names}
            repopool.write_target_repos(target.portage_dir, locations, target.title)
            changed.append(repopool.target_file(target.portage_dir))
    return changed


def repo_folder(name, folder):
    """Use a folder of the user's in place of a repository of Porthole's
    list (a working copy with fixes not pushed yet), never synced, for the
    systems using it too; folder None: back to the synced checkout"""
    import configparser
    from porthole.updater import repopool
    if name not in repopool.repos():
        raise JobError("%s isn't one of the repositories for other systems" % name)
    if folder:
        folder = check_folder(folder)
        own = repo_own_name(folder)
        if own and own != name:
            raise JobError("That repository calls itself %s (profiles/repo_name), not %s"
                           % (own, name))
    try:
        location = repopool.use_folder(name, folder)
    except ValueError as e:
        raise JobError(str(e))
    changed = [repopool.POOL_FILE]
    for target in targets.load().values():
        if target.local or name not in repopool.target_repos(target.portage_dir):
            continue
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(repopool.target_file(target.portage_dir))
        locations = {n: (location if n == name else parser.get(n, "location"))
                     for n in parser.sections()}
        repopool.write_target_repos(target.portage_dir, locations, target.title)
        # where a chroot into the system bind-mounts it
        os.makedirs(os.path.join(target.root, location.lstrip("/")), mode=0o755,
                    exist_ok=True)
        changed.append(repopool.target_file(target.portage_dir))
    return ["%s now from %s" % (name, location)] + changed


def check_folder(path):
    """A folder already on this computer holding a repository (the user's
    own checkout): its real path. Porthole never syncs or deletes it."""
    if not path or not os.path.isabs(path) or "\n" in path:
        raise JobError("A folder is a full path: %r" % (path or "")[:80])
    real = os.path.realpath(path)
    if not os.path.isdir(real) or real == "/":
        raise JobError("Not a folder: %s" % path)
    if not os.path.isfile(os.path.join(real, "profiles", "repo_name")) and \
            not os.path.isfile(os.path.join(real, "metadata", "layout.conf")):
        raise JobError("%s doesn't look like a repository (no profiles/repo_name or "
                       "metadata/layout.conf)" % real)
    return real


def all_repo_names(target=None):
    """Names taken: this computer's, the list for other systems, and the
    system's own"""
    from porthole.updater import repopool, system
    names = set(system.repos_conf_sections()) | set(repopool.repos()) | {"gentoo"}
    if target is not None and not target.local:
        names |= set(system.repos_conf_sections(target.portage_dir))
    return names


def add_repo(name, sync_type, uri, priority=None, auto_sync=True, target_name=None,
             folder=None):
    """Add a repository and queue its first sync (None for a folder).

    For this computer: repos.conf/<name>.conf (one file each, the way
    most people write them). For other systems (target_name SYSTEMS, or a
    system's name, which then uses it): Porthole's own list, never this
    computer's repos.conf."""
    from porthole.updater import repopool, system
    if not system._REPO_NAME.match(name or ""):
        raise JobError("Not a repository name: %s" % name)
    if folder:
        location = check_folder(folder)
        own = repo_own_name(location)
        if own and own != name:
            raise JobError("That repository calls itself %s (profiles/repo_name); portage "
                           "only uses it under that name" % own)
    else:
        if sync_type not in system.SYNC_TYPES:
            raise JobError("Not a sync type portage knows: %s" % sync_type)
        try:
            system.check_repo_uri(uri)
        except ValueError as e:
            raise JobError(str(e))
        location = os.path.join(REPOS_DIR, name)
    target = None
    if target_name != SYSTEMS:
        try:
            target = targets.get(target_name)
        except KeyError:
            raise JobError("No such system: %s" % target_name)
    if name in all_repo_names(target):
        raise JobError("There already is a repository called %s" % name)
    if target is None or not target.local:
        if not folder and os.path.lexists(location):
            raise JobError("%s is there already; add it as a folder, or move it away"
                           % location)
        try:
            repopool.add(name, location, "" if folder else sync_type,
                         "" if folder else uri, auto_sync)
        except ValueError as e:
            raise JobError(str(e))
        if target is not None:
            use_repo(name, target.name)
        return None if folder else submit_repo_sync(name)
    directory = os.path.join(target.portage_dir, "repos.conf")
    if os.path.isfile(directory):
        raise JobError("%s is a single file. Porthole adds each repository in a file of "
                       "its own: move it into a new directory of that name first."
                       % directory)
    if folder:
        lines = ["[%s]" % name, "location = %s" % location, "auto-sync = no"]
    else:
        lines = ["[%s]" % name, "location = %s" % location,
                 "sync-type = %s" % sync_type, "sync-uri = %s" % uri,
                 "auto-sync = %s" % ("yes" if auto_sync else "no")]
    if priority is not None:
        lines.append("priority = %d" % int(priority))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name + ".conf")
    with open(path + ".new", "w") as f:
        f.write("# Added in Porthole (Settings, Repositories)\n" + "\n".join(lines) + "\n")
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return None if folder else submit_repo_sync(name)


def disable_repo(name, portage_dir="/etc/portage"):
    """Stop this computer using a repository and put it on Porthole's list
    for other systems instead (its checkout stays where it is, systems may
    still use it). Only a repository with a repos.conf file of its own."""
    import configparser
    from porthole.updater import repopool, system
    if not system._REPO_NAME.match(name or "") or name == "gentoo":
        raise JobError("Not a repository Porthole can move: %s" % name)
    sections = system.repos_conf_sections(portage_dir)
    if name not in sections:
        raise JobError("%s isn't in this computer's repos.conf" % name)
    path = sections[name]
    if [n for n, f in sections.items() if f == path and n != name]:
        raise JobError("%s defines other repositories too; move [%s] out of it by hand"
                       % (path, name))
    if name in repopool.repos():
        raise JobError("%s is on the list for other systems already" % name)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    section = parser[name]
    location = section.get("location", "").strip() or os.path.join(REPOS_DIR, name)
    sync_type = section.get("sync-type", "").strip()
    repopool.add(name, location.rstrip("/"), sync_type,
                 section.get("sync-uri", "").strip() if sync_type else "",
                 section.get("auto-sync", "yes").strip().lower() in ("yes", "true", "1"))
    os.unlink(path)
    return ["This computer no longer uses %s (kept for other systems)" % name,
            "removed " + path]


def enable_repo(name, portage_dir="/etc/portage"):
    """Let this computer use a repository from Porthole's list: it moves to
    repos.conf/<name>.conf (systems using it carry on, same path)"""
    from porthole.updater import repopool, system
    pool = repopool.repos()
    if name not in pool:
        raise JobError("%s isn't on the list for other systems" % name)
    if name in system.repos_conf_sections(portage_dir) or name == "gentoo":
        raise JobError("This computer has a repository called %s already" % name)
    directory = os.path.join(portage_dir, "repos.conf")
    if os.path.isfile(directory):
        raise JobError("%s is a single file. Porthole adds each repository in a file of "
                       "its own: move it into a new directory of that name first."
                       % directory)
    repo = pool[name]
    lines = ["[%s]" % name, "location = %s" % repo["location"]]
    if repo["sync_type"]:
        lines += ["sync-type = %s" % repo["sync_type"], "sync-uri = %s" % repo["sync_uri"],
                  "auto-sync = %s" % ("yes" if repo["auto_sync"] else "no")]
    else:
        lines.append("auto-sync = no")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name + ".conf")
    with open(path + ".new", "w") as f:
        f.write("# Added in Porthole (Settings, Repositories)\n" + "\n".join(lines) + "\n")
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    repopool.remove(name)
    return ["This computer uses %s now" % name, "wrote " + path]


def _system_target(target_name):
    try:
        target = targets.get(target_name)
    except KeyError:
        raise JobError("No such system: %s" % target_name)
    if target.local:
        raise JobError("This computer's repositories are its repos.conf")
    return target


def use_repo(name, target_name):
    """Let a system use one of this computer's repositories, or one from
    the list for other systems (at the same path)"""
    from porthole.updater import repopool
    target = _system_target(target_name)
    available = roots_repos()
    if name not in available:
        raise JobError("No repository called %s on this computer" % name)
    current = repopool.target_repos(target.portage_dir)
    if name in current:
        return "already used"
    locations = {n: available[n] for n in current if n in available}
    locations.setdefault("gentoo", available.get("gentoo", "/var/db/repos/gentoo"))
    locations[name] = available[name]
    repopool.write_target_repos(target.portage_dir, locations, target.title)
    return "used"


def drop_repo(name, target_name):
    """Stop a system using a repository (the repository stays)"""
    from porthole.updater import repopool
    target = _system_target(target_name)
    if name == "gentoo":
        raise JobError("Every system uses gentoo")
    current = repopool.target_repos(target.portage_dir)
    if name not in current:
        raise JobError("%s doesn't use %s" % (target.title, name))
    available = roots_repos()
    locations = {n: available[n] for n in current if n != name and n in available}
    repopool.write_target_repos(target.portage_dir, locations, target.title)
    return "dropped"


def deletable(location):
    """A repository checkout Porthole may delete: directly in /var/db/repos,
    and not Gentoo's own"""
    real = os.path.realpath(location or "/")
    return (os.path.dirname(real) == REPOS_DIR
            and real != os.path.join(REPOS_DIR, "gentoo"))


def remove_repo(name, delete_files=False, target_name=None):
    """Remove a repository's definition: only a repos.conf file of its own
    (one defining others too is left for the user to edit). delete_files:
    its checkout too, only if that is directly in /var/db/repos.
    target_name SYSTEMS: one from the list for other systems, which every
    system stops using."""
    from porthole.updater import repopool, system
    if not system._REPO_NAME.match(name or ""):
        raise JobError("Not a repository name: %s" % name)
    if target_name == SYSTEMS:
        pool = repopool.repos()
        if name not in pool:
            raise JobError("%s isn't one of the repositories for other systems" % name)
        removed = []
        for target in targets.load().values():
            if not target.local and name in repopool.target_repos(target.portage_dir):
                drop_repo(name, target.name)
                removed.append("%s no longer uses it" % target.title)
        repopool.remove(name)
        removed.append(repopool.POOL_FILE)
        real = os.path.realpath(pool[name]["location"])
        if delete_files and pool[name]["sync_type"] and deletable(real) and os.path.isdir(real):
            import shutil
            shutil.rmtree(real)
            removed.append(real)
        return removed
    try:
        target = targets.get(target_name)
    except KeyError:
        raise JobError("No such system: %s" % target_name)
    sections = system.repos_conf_sections(target.portage_dir)
    if name == "gentoo" or name not in sections:
        raise JobError("%s can't be removed here" % name if name == "gentoo"
                       else "%s isn't defined in repos.conf" % name)
    path = sections[name]
    others = [s for s, f in sections.items() if f == path and s != name]
    if others:
        raise JobError("%s also defines %s; remove [%s] from it by hand."
                       % (path, ", ".join(others), name))
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    location = parser.get(name, "location", fallback="")
    os.unlink(path)
    removed = [path]
    if delete_files and location:
        real = os.path.realpath(location)
        if (deletable(real) and os.path.isdir(real)):
            import shutil
            shutil.rmtree(real)
            removed.append(real)
    return removed


# -- binary packages ---------------------------------------------------------------

def add_binhost(name, uri, priority=None, verify=True, target_name=None):
    """A binhost in binrepos.conf/<name>.conf (one file each)"""
    from porthole.updater import system
    if not system.BINHOST_NAME.match(name or ""):
        raise JobError("Not a binhost name: %s" % name)
    try:
        system.check_binhost_uri(uri)
        target = targets.get(target_name)
    except ValueError as e:
        raise JobError(str(e))
    except KeyError:
        raise JobError("No such system: %s" % target_name)
    if name in {b["name"] for b in system.binrepos(target.portage_dir)}:
        raise JobError("There already is a binhost called %s" % name)
    directory = os.path.join(target.portage_dir, "binrepos.conf")
    if os.path.isfile(directory):
        raise JobError("%s is a single file. Porthole adds each binhost in a file of its "
                       "own: move it into a new directory of that name first." % directory)
    lines = ["[%s]" % name, "sync-uri = %s" % uri,
             "location = /var/cache/binhost/%s" % name,
             "verify-signature = %s" % ("true" if verify else "false")]
    if priority is not None:
        lines.append("priority = %d" % int(priority))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name + ".conf")
    with open(path + ".new", "w") as f:
        f.write("# Added in Porthole (Settings, Binary packages)\n" + "\n".join(lines) + "\n")
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return path


def remove_binhost(name, target_name=None):
    """Remove a binhost: only a binrepos.conf file of its own"""
    from porthole.updater import system
    try:
        target = targets.get(target_name)
    except KeyError:
        raise JobError("No such system: %s" % target_name)
    entries = system.binrepos(target.portage_dir)
    mine = [b for b in entries if b["name"] == name]
    if not mine:
        raise JobError("%s isn't in binrepos.conf" % name)
    path = mine[0]["file"]
    others = [b["name"] for b in entries if b["file"] == path and b["name"] != name]
    if others or os.path.basename(path) == "binrepos.conf":
        raise JobError("%s also defines %s; remove [%s] from it by hand."
                       % (path, ", ".join(others) or "other settings", name))
    os.unlink(path)
    return path


def submit_binpkg_job(action):
    """Look after the package directory: clean (eclean-pkg), index
    (emaint binhost --fix) or upload (rsync to binpkg_upload)"""
    import shutil
    if action == "clean":
        if not shutil.which("eclean-pkg"):
            raise JobError("Cleaning needs eclean-pkg (app-portage/gentoolkit)")
        argv, title = ["eclean-pkg", "--deep"], "Clean old binary packages"
    elif action == "index":
        argv, title = ["emaint", "binhost", "--fix"], "Rebuild the binary package index"
    elif action == "upload":
        from porthole.updater import system
        destination = load_config().get("binpkg_upload")
        if not destination:
            raise JobError("No upload destination is set (Settings, Binary packages)")
        system.check_upload_destination(destination)
        if not shutil.which("rsync"):
            raise JobError("Uploading needs rsync (net-misc/rsync)")
        pkgdir = system.pkgdir_stats()[0]
        argv = ["rsync", "-a", "--delete", pkgdir.rstrip("/") + "/", destination]
        title = "Upload binary packages to %s" % destination
    else:
        raise JobError("Not a binary package job: %s" % action)
    job = new_job("binpkg", title, [{"kind": "argv", "title": title, "argv": argv}],
                  recheck=False)
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
        elif key == "plugins":
            from porthole import plugins
            names = value.split()
            unknown = [n for n in names if n not in plugins.installed()]
            if unknown:
                raise JobError("No such plugin installed: %s" % " ".join(unknown))
            clean[key] = " ".join(sorted(set(names)))
        elif key == "binpkg_upload":
            from porthole.updater.system import check_upload_destination
            try:
                clean[key] = check_upload_destination(value) if value else ""
            except ValueError as e:
                raise JobError(str(e))
        elif key == "automation":
            from porthole.updater.service import AUTOMATION
            if value not in AUTOMATION:
                raise JobError("automation must be one of: " + ", ".join(AUTOMATION))
            clean[key] = value
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
    current = load_config(path)
    level = clean.get("automation", current["automation"])
    window = (clean["build_window"] if "build_window" in clean else current["build_window"])
    if level in ("approve", "safe", "auto") and not window:
        raise JobError("Building in the build window needs a build window")
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
