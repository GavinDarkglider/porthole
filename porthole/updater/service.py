"""
    Porthole update service

    The background half of the update assistant, run as root from cron
    (hourly is fine: it decides itself whether a check is due). When due
    it syncs the repositories (at most once a day), works out an update
    plan with the analyzer and publishes it for the notifier:

        /var/lib/porthole/plan.json    the latest plan (world readable)
        /var/lib/porthole/state.json   last check/sync times, running flag

    The check itself never builds packages or writes to /etc/portage.
    Updates the user approved run through the job runner (runner.py):

        submit update --plan TIME   queue the reviewed plan
        submit check [--sync-now]   queue a sync and check
        submit emerge -- CMD...     queue an emerge from Porthole (checked)
        submit install --request J  plan an install; queue it unless it needs review
        submit install --plan F --generated T   queue a reviewed install plan
        cancel [JOB]                cancel queued jobs, stop the running one
        jobs                        show the queue
        config [KEY=VALUE...]       show or change updater.conf

    Settings live in /etc/porthole/updater.conf:

        [updater]
        # sync the repositories every this many days, 1 or more
        interval_days = 7
        # check for updates after each sync
        check_updates = yes
        # don't sync while running on battery
        skip_on_battery = yes
        # check live (-9999) packages for new upstream commits
        # (smart-live-rebuild) and plan rebuilding the changed ones
        live_rebuild = yes
        # live packages never to rebuild this way (cat/pkg, space separated)
        live_exclude =
        # list packages nothing needs any more (emerge --depclean), so the
        # update assistant can offer to remove them after the update
        offer_depclean = yes

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import argparse
import configparser
import fcntl
import glob
import json
import os
import subprocess
import sys
import time

from porthole.updater.analyze import WORLD_ARGS as WORLD_TARGETS, Analyzer, format_report

CONFIG_FILE = "/etc/porthole/updater.conf"
STATE_DIR = "/var/lib/porthole"
PLAN_FILE = os.path.join(STATE_DIR, "plan.json")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
# the user's standing choices: updates to hold, changes to refuse (runner.py)
CHOICES_FILE = os.path.join(STATE_DIR, "choices.json")
# an update plan the user worked out themselves (Check now), without root
USER_PLAN_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "porthole", "update-plan.json")


def latest_plan():
    """(plan, path): the newer of the background check's plan and the
    user's own"""
    import datetime

    def when(plan):
        try:
            return datetime.datetime.fromisoformat(plan["generated"]).timestamp()
        except (KeyError, TypeError, ValueError):
            return 0
    plans = [(read_json(path), path) for path in (PLAN_FILE, USER_PLAN_FILE)]
    plans = [(plan, path) for plan, path in plans if plan]
    if not plans:
        return None, PLAN_FILE
    return max(plans, key=lambda pp: when(pp[0]))


def world_request(choices=None):
    """What a world update check asks the planner for"""
    config = load_config()
    return {"targets": list(WORLD_TARGETS), "requested": [],
            "choices": choices if choices is not None else read_json(CHOICES_FILE, {}) or {},
            "live": config["live_rebuild"], "live_exclude": config["live_exclude"],
            "depclean": config["offer_depclean"]}
LOG_FILE = "/var/log/porthole/updater.log"
LOCK_FILE = "/run/lock/porthole-updater.lock"

DAY = 24 * 3600
# cron fires on the hour; don't skip a day because the last run ended late
SLACK = 3600

DEFAULTS = {
    "interval_days": "7",
    "check_updates": "yes",
    "skip_on_battery": "yes",
    "live_rebuild": "yes",
    "live_exclude": "",
    "offer_depclean": "yes",
}


def load_config(path=CONFIG_FILE):
    parser = configparser.ConfigParser()
    parser.read_dict({"updater": DEFAULTS})
    parser.read(path)
    section = parser["updater"]
    try:
        interval = int(section.get("interval_days"))
    except ValueError:
        interval = int(DEFAULTS["interval_days"])
    return {
        # syncing more than once a day is unfair to the mirrors
        "interval_days": max(1, interval),
        "check_updates": section.getboolean("check_updates", True),
        "skip_on_battery": section.getboolean("skip_on_battery", True),
        "live_rebuild": section.getboolean("live_rebuild", True),
        "live_exclude": section.get("live_exclude", "").split(),
        "offer_depclean": section.getboolean("offer_depclean", True),
    }


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Write atomically so readers never see half a file"""
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class Log:
    def __init__(self, path=LOG_FILE, echo=True):
        self.echo = echo
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.file = open(path, "a")
        except OSError:
            self.file = None

    def __call__(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()
        if self.echo:
            print(msg, file=sys.stderr, flush=True)


def emerge_running():
    """True if an emerge process is running (we'd race its changes)"""
    me = os.getpid()
    for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            if int(cmdline.split("/")[2]) == me:
                continue
            with open(cmdline, "rb") as f:
                args = f.read().split(b"\0")
        except (OSError, ValueError):
            continue
        # python3 /usr/lib/python-exec/python3.x/emerge ... or emerge ...
        if any(os.path.basename(a) == b"emerge" for a in args[:2]):
            return True
    return False


def on_battery():
    """True when running on battery with no AC adapter online"""
    supplies = glob.glob("/sys/class/power_supply/*")
    mains = [s for s in supplies if _read(os.path.join(s, "type")) == "Mains"]
    if not mains:
        return False  # desktop, or unknown
    return not any(_read(os.path.join(s, "online")) == "1" for s in mains)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def last_repo_sync():
    """Newest repository sync time, including syncs run by hand"""
    import portage
    times = []
    for repo in portage.db[portage.root]["vartree"].settings.repositories:
        for name in ("metadata/timestamp.chk", "metadata/timestamp.x", ".git/FETCH_HEAD"):
            path = os.path.join(repo.location, name)
            if os.path.exists(path):
                times.append(os.path.getmtime(path))
                break
    return max(times) if times else None


def security_packages(log):
    """Installed packages affected by a GLSA not yet applied (@security),
    which only root can work out; for Porthole's Sets view. None if it
    couldn't be worked out."""
    try:
        import portage
        from portage._sets import load_default_config
        config = load_default_config(portage.settings, portage.db[portage.root])
        return sorted({atom.cp for atom in config.getSetAtoms("security")})
    except Exception as e:
        log("Couldn't work out @security: %s" % e)
        return None


def check_lock(wait=True):
    """Hold this while checking, so two checks never overlap. Returns
    the open lock file, or None if not waiting and it is taken."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        lock.close()
        return None
    return lock


class Service:
    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.state = read_json(STATE_FILE, {}) or {}

    def save_state(self, **changes):
        self.state.update(changes)
        write_json(STATE_FILE, self.state)

    def due(self):
        """A sync is due interval_days after the last one, whoever ran it"""
        last = max(filter(None, [last_repo_sync(), self.state.get("last_sync")]),
                   default=None)
        if not last:
            return True
        return time.time() - last >= self.config["interval_days"] * DAY - SLACK

    def sync(self, now=False):
        """Sync unless any repository was synced within the last day, or
        now (the user asked for it, having been told when it last was)"""
        recent = max(filter(None, [last_repo_sync(), self.state.get("last_sync")]),
                     default=None)
        if recent and time.time() - recent < DAY - SLACK and not now:
            self.log("Repositories synced %.1f hours ago; not syncing again yet"
                     % ((time.time() - recent) / 3600))
            return {"ran": False, "ok": True, "last": recent}
        self.log("Syncing repositories (emaint sync --auto)...")
        try:
            proc = subprocess.run(
                ["emaint", "sync", "--auto"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=3600,
            )
            output, ok = proc.stdout, proc.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            output, ok = str(e), False
        if self.log.file:
            self.log.file.write(output)
        now = time.time()
        if ok:
            self.save_state(last_sync=now)
            self.log("Sync finished")
        else:
            self.log("Sync failed; checking against the current tree")
        return {"ran": True, "ok": ok, "last": now if ok else recent,
                "error": None if ok else output[-2000:]}

    def run(self, force=False, sync=True, from_runner=False):
        if not from_runner:
            from porthole.updater.runner import runner_active
            if runner_active():
                self.log("Update jobs are running; checking later")
                return 0
        if not force:
            if not self.due():
                self.log("Not due yet")
                return 0
            if self.config["skip_on_battery"] and on_battery():
                self.log("On battery; trying again later")
                return 0
        if emerge_running():
            self.log("emerge is running; trying again later")
            return 0

        self.save_state(running=True, started=time.time())
        try:
            sync_info = None
            if sync:
                sync_info = self.sync(now=sync == "now")
            # a scheduled sync checks for updates only if asked to; a
            # check someone asked for (force) always does
            if not force and not self.config["check_updates"]:
                self.log("Synced; checking for updates is turned off")
                return 0 if (sync_info or {}).get("ok", True) else 1
            plan = Analyzer(log=self.log, choices=read_json(CHOICES_FILE),
                            live=self.config["live_rebuild"],
                            live_exclude=self.config["live_exclude"],
                            depclean=self.config["offer_depclean"]).analyze()
            plan["sync"] = sync_info
            plan["security"] = security_packages(self.log)
            write_json(PLAN_FILE, plan)
            self.log("Plan written: %s, %d updates, %d old-library rebuilds, "
                     "%d live rebuilds"
                     % (plan.get("status"), len(plan.get("merge_list") or []),
                        len((plan.get("preserved") or {}).get("merge_list") or []),
                        len((plan.get("live") or {}).get("changed") or [])))
            self.save_state(last_check=time.time(), last_status=plan.get("status"))
            return 0 if plan.get("status") != "error" else 1
        finally:
            self.save_state(running=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Porthole update service (run as root).")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="sync (and check for updates) if due")
    run.add_argument("--force", action="store_true",
                     help="sync and check now, even if not due or on battery "
                          "(the once-a-day sync limit still applies)")
    run.add_argument("--no-sync", action="store_true", help="don't sync first")
    run.add_argument("--quiet", action="store_true", help="only write the log file")
    sub.add_parser("status", help="show the last plan")
    submit = sub.add_parser("submit", help="queue a job for the runner")
    submit.add_argument("kind", choices=("update", "check", "emerge", "install"))
    submit.add_argument("--plan", help="update: the 'generated' time of the plan reviewed; "
                                       "install: the reviewed plan file")
    submit.add_argument("--generated", help="install --plan: its 'generated' time")
    submit.add_argument("--request", help="install: what to install (JSON)")
    submit.add_argument("--expect", help="install --request: fingerprint of the plan "
                                         "the user approved; install only that")
    submit.add_argument("--no-sync", action="store_true", help="check: don't sync first")
    submit.add_argument("--sync-now", action="store_true",
                        help="check: sync even if synced within the last day")
    submit.add_argument("--choices", help="check: replace the standing choices (JSON)")
    # after "--", so its options aren't taken for submit's own
    submit.add_argument("cmdline", nargs="*", metavar="CMD",
                        help="emerge: the command line, after --")
    cancel = sub.add_parser("cancel", help="cancel queued jobs and stop the running one")
    cancel.add_argument("job", nargs="?", help="only this job")
    cancel.add_argument("--drop", action="store_true",
                        help="don't offer what an install didn't get to again")
    sub.add_parser("jobs", help="show the job queue")
    conf = sub.add_parser("config", help="show or change updater.conf")
    conf.add_argument("settings", nargs="*", metavar="KEY=VALUE")
    news = sub.add_parser("news", help="mark Gentoo news items read or unread")
    news.add_argument("state", choices=("read", "unread"))
    news.add_argument("--repo", default="gentoo")
    news.add_argument("names", nargs="+", metavar="ITEM")
    sub.add_parser("runner", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command == "jobs":
        from porthole.updater.runner import JOBS_FILE
        for job in (read_json(JOBS_FILE, {}) or {}).get("jobs", []):
            progress = job.get("progress") or {}
            print("%s  %-9s %s%s" % (job["id"], job["status"], job["title"],
                                     "  (%d of %d)" % (progress["done"], progress["total"])
                                     if progress and job["status"] == "running" else ""))
        return 0
    if args.command == "config" and not args.settings:
        for key, value in load_config().items():
            print("%s = %s" % (key, " ".join(value) if isinstance(value, list) else value))
        return 0

    if args.command == "status":
        plan = read_json(PLAN_FILE)
        state = read_json(STATE_FILE, {})
        if state.get("running"):
            print("A check is running now.")
        if not plan:
            print("No plan yet.")
            return 1
        print("Checked %s\n" % plan.get("generated"))
        print(format_report(plan))
        return 0

    if os.geteuid() != 0:
        parser.error("%s needs root" % args.command)
    if args.command != "run":
        return job_command(args, parser)
    lock = check_lock(wait=False)
    if lock is None:
        print("Another check is already running.", file=sys.stderr)
        return 0
    log = Log(echo=not args.quiet)
    return Service(load_config(), log).run(force=args.force, sync=not args.no_sync)


def job_command(args, parser):
    """submit, cancel, config and the runner itself"""
    from porthole.updater import runner
    if args.command == "runner":
        return runner.Runner().run()
    if args.command == "news":
        from porthole.updater import news
        try:
            left = news.set_read(args.repo, args.names, args.state == "read")
        except (ValueError, OSError) as e:
            print(e, file=sys.stderr)
            return 3
        print("%d unread" % left)
        return 0
    try:
        if args.command == "submit":
            if args.kind == "install":
                # prints "queued JOB" or "review PLAN_FILE"
                if args.plan:
                    outcome, what = "queued", runner.submit_install_plan(
                        args.plan, args.generated)
                elif args.request:
                    outcome, what = runner.submit_install(args.request, args.expect)
                else:
                    parser.error("submit install needs --request or --plan")
                if outcome == "queued":
                    runner.start_runner()
                    print("queued", what["id"])
                else:
                    print("review", what)
                return 0
            if args.kind == "emerge":
                tokens = args.cmdline[1:] if args.cmdline[:1] == ["--"] else args.cmdline
                job = runner.submit_emerge(tokens)
            elif args.kind == "update":
                if not args.plan:
                    parser.error("submit update needs --plan")
                job = runner.submit_update(args.plan)
            else:
                if args.choices is not None:
                    runner.set_choices(args.choices)
                job = runner.submit_check(sync="now" if args.sync_now else not args.no_sync)
            runner.start_runner()
            print(job["id"])
        elif args.command == "cancel":
            stopped = runner.cancel(args.job, keep_rest=not args.drop)
            print("\n".join(stopped) if stopped else "Nothing to cancel.")
        elif args.command == "config":
            changes = {}
            for setting in args.settings:
                key, sep, value = setting.partition("=")
                if not sep:
                    parser.error("settings are KEY=VALUE")
                changes[key.strip()] = value.strip()
            for key, value in runner.set_config(changes).items():
                print("%s = %s" % (key, " ".join(value) if isinstance(value, list) else value))
    except runner.JobError as e:
        print(e, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
