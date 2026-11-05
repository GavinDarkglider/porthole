"""
    Porthole: services a job needs, running only while it runs

    A job can name helpers ("helpers": [...]). The runner starts them
    before the job's first step and stops them after its last, whatever
    happened, so nothing is left listening or registered between builds:

      distcc   a distccd of the job's own, on 127.0.0.1 only, on a free
               port, running only the compilers distcc's masquerade
               directory lists (/usr/lib/distcc, distcc's default). Builds
               in an emulated root hand their compiling to it, so they run
               at native speed. The computer's own distccd service and its
               settings are left alone.
      binfmt   qemu's registration of other architectures' programs
               (OpenRC's qemu-binfmt), started if it wasn't running and
               stopped again only if Porthole started it.

    start() returns environment settings for the job's steps (DISTCC_HOSTS
    for the job's distccd).

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import shutil
import signal
import socket
import subprocess
import time

RUN_DIR = "/run/porthole"
LOG_DIR = "/var/log/porthole"


class HelperError(Exception):
    pass


def free_port():
    """A TCP port nothing listens on at 127.0.0.1 just now"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _is_distccd(pid):
    try:
        with open("/proc/%d/comm" % pid) as f:
            return f.read().strip() == "distccd"
    except OSError:
        return False


def safe_jobs(threads=None, memory_gb=None):
    """Compile jobs this computer can run at once: one per thread, but no
    more than one per 2 GB of memory"""
    threads = threads or os.cpu_count() or 1
    if memory_gb is None:
        try:
            with open("/proc/meminfo") as f:
                kb = next(int(l.split()[1]) for l in f if l.startswith("MemTotal:"))
            memory_gb = kb // (1024 * 1024)
        except (OSError, StopIteration, ValueError):
            memory_gb = 2 * threads
    return max(1, min(threads, memory_gb // 2))


def _daemons(match):
    """[pid] of distccd daemons (not their workers) whose --pid-file matches"""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                args = f.read().decode(errors="replace").split("\0")
            with open("/proc/%d/stat" % pid) as f:
                parent = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if not args or os.path.basename(args[0]) != "distccd" or "--pid-file" not in args:
            continue
        pid_file = args[args.index("--pid-file") + 1] if args.index("--pid-file") + 1 < len(
            args) else ""
        # a worker's parent is a distccd too: count the daemon only
        if match(pid_file) and not _is_distccd(parent):
            found.append(pid)
    return found


def stop_strays(run_dir, log=lambda m: None):
    """Stop distccd daemons left by Porthole jobs that ended without
    stopping them (their pid files are in run_dir)"""
    prefix = os.path.join(run_dir, "distccd-")
    for pid in _daemons(lambda pid_file: pid_file.startswith(prefix)):
        try:
            os.kill(pid, signal.SIGTERM)
            log("== distcc: stopped one left by an earlier job (pid %d)" % pid)
        except OSError:
            pass


class LocalDistcc:
    """A distccd for one job, on 127.0.0.1"""

    name = "distcc"

    def __init__(self, job_id, run_dir=None, log_dir=None, jobs=None):
        self.job_id = job_id
        run_dir, log_dir = run_dir or RUN_DIR, log_dir or LOG_DIR
        self.pid_file = os.path.join(run_dir, "distccd-%s.pid" % job_id)
        self.log_file = os.path.join(log_dir, "distccd-%s.log" % job_id)
        self.jobs = jobs or safe_jobs()
        self.port = None

    def start(self, log):
        distccd = shutil.which("distccd")
        if not distccd:
            raise HelperError("distcc isn't installed (sys-devel/distcc)")
        os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        self.port = free_port()
        argv = [distccd, "--daemon", "--listen", "127.0.0.1", "--allow", "127.0.0.1",
                "--port", str(self.port), "--jobs", str(self.jobs),
                "--pid-file", self.pid_file, "--log-file", self.log_file,
                # info: one summary line per compile (COMPILE_OK / _ERROR),
                # counted when it stops
                "--log-level", "info"]
        stop_strays(os.path.dirname(self.pid_file), log)
        if os.geteuid() == 0:
            # distccd won't compile as root, and writes its pid file and log
            # as distcc: both are made for it first (the folder stays root's)
            argv += ["--user", "distcc"]
            try:
                import pwd
                user = pwd.getpwnam("distcc")
                for path in (self.pid_file, self.log_file):
                    open(path, "a").close()
                    os.chown(path, user.pw_uid, user.pw_gid)
            except (KeyError, OSError) as e:
                raise HelperError("distcc's user isn't set up: %s" % e)
        result = subprocess.run(argv, capture_output=True, text=True)
        from porthole.updater import recipe
        recipe.note("distccd: a compile server on this computer, for this job only. The\n"
                    "build's compiler names (gcc, aarch64-...-gcc) lead to distcc, which\n"
                    "hands each compile to it.", once="distccd")
        recipe.ran(argv, result.returncode)
        if result.returncode != 0:
            raise HelperError("distccd didn't start: %s" % (result.stderr.strip()
                                                             or result.returncode))
        # --daemon returns before it listens
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.stop(log)
            raise HelperError("distccd started but doesn't answer on port %d" % self.port)
        log("== distcc: compiling on 127.0.0.1:%d for this job (%d at a time)"
            % (self.port, self.jobs))
        # the build hands its compiling to this distccd: as many jobs as it
        # runs, and none started while this computer's load is above its threads
        return {"DISTCC_HOSTS": "127.0.0.1:%d/%d" % (self.port, self.jobs),
                "MAKEOPTS": "-j%d -l%d" % (self.jobs, os.cpu_count() or 1)}

    def pid(self):
        try:
            with open(self.pid_file) as f:
                pid = int(f.read().strip())
            if _is_distccd(pid):
                return pid
        except (OSError, ValueError):
            pass
        # no pid file (or a stale one): the daemon by its own --pid-file
        found = _daemons(lambda pid_file: pid_file == self.pid_file)
        return found[0] if found else None

    def stop(self, log):
        pid = self.pid()
        if pid:
            # distccd's parent takes its children with it
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _is_distccd(pid):
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
            log("== distcc: stopped%s" % self.summary())
        try:
            os.unlink(self.pid_file)
        except OSError:
            pass

    def summary(self):
        """", N compiles done here (M failed)" from its log, or "" """
        try:
            with open(self.log_file, errors="replace") as f:
                text = f.read()
        except OSError:
            return ""
        ok, failed = text.count("COMPILE_OK"), text.count("COMPILE_ERROR")
        if not ok and not failed:
            return ", no compiles were sent to it"
        return ", %d compile%s done here%s" % (ok, "" if ok == 1 else "s",
                                                " (%d failed)" % failed if failed else "")


class Binfmt:
    """qemu's binfmt registration (OpenRC's qemu-binfmt service)"""

    name = "binfmt"
    SERVICE = "qemu-binfmt"

    def __init__(self, job_id=None):
        self.started = False

    @classmethod
    def available(cls):
        """How it can be registered here: "openrc", "systemd" (systemd-binfmt
        reads qemu's binfmt.d files at boot), or None"""
        if os.path.exists("/etc/init.d/" + cls.SERVICE):
            return "openrc"
        if os.path.isdir("/run/systemd/system") and any(
                f.startswith("qemu-") for d in ("/usr/lib/binfmt.d", "/etc/binfmt.d")
                if os.path.isdir(d) for f in os.listdir(d)):
            return "systemd"
        return None

    def start(self, log):
        how = self.available()
        if how == "systemd":
            return {}  # registered at boot by systemd-binfmt
        if how is None:
            raise HelperError("qemu's binfmt service isn't installed (app-emulation/qemu)")
        running = subprocess.run(["rc-service", self.SERVICE, "status"],
                                 capture_output=True).returncode == 0
        if not running:
            result = subprocess.run(["rc-service", self.SERVICE, "start"],
                                    capture_output=True, text=True)
            from porthole.updater import recipe
            recipe.note("binfmt_misc: the kernel hands programs built for another CPU\n"
                        "to qemu, so the system's own programs run here.", once="binfmt")
            recipe.ran(["rc-service", self.SERVICE, "start"], result.returncode)
            if result.returncode != 0:
                raise HelperError("%s didn't start: %s" % (
                    self.SERVICE, (result.stdout + result.stderr).strip()))
            self.started = True
            log("== binfmt: started %s for this job" % self.SERVICE)
        return {}

    def stop(self, log):
        if self.started:
            subprocess.run(["rc-service", self.SERVICE, "stop"], capture_output=True)
            log("== binfmt: stopped %s" % self.SERVICE)
            self.started = False


HELPERS = {"distcc": LocalDistcc, "binfmt": Binfmt}


def check_helpers(names):
    """The helper names a job may ask for; ValueError for others"""
    names = list(names or [])
    unknown = [n for n in names if n not in HELPERS]
    if unknown:
        raise ValueError("No such helper: %s" % " ".join(map(str, unknown)))
    return names


class Helpers:
    """The helpers of one job: start all (stopping those started if one
    fails), stop all"""

    def __init__(self, job_id, names, **kwargs):
        self.items = [HELPERS[n](job_id, **kwargs.get(n, {})) for n in check_helpers(names)]
        self.running = []

    def start(self, log):
        env = {}
        for helper in self.items:
            try:
                env.update(helper.start(log) or {})
            except Exception:
                self.stop(log)
                raise
            self.running.append(helper)
        return env

    def stop(self, log):
        while self.running:
            helper = self.running.pop()
            try:
                helper.stop(log)
            except Exception as e:
                log("!! couldn't stop %s: %s" % (helper.name, e))
