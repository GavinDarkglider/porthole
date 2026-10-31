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


class LocalDistcc:
    """A distccd for one job, on 127.0.0.1"""

    name = "distcc"

    def __init__(self, job_id, run_dir=None, log_dir=None, jobs=None):
        self.job_id = job_id
        run_dir, log_dir = run_dir or RUN_DIR, log_dir or LOG_DIR
        self.pid_file = os.path.join(run_dir, "distccd-%s.pid" % job_id)
        self.log_file = os.path.join(log_dir, "distccd-%s.log" % job_id)
        self.jobs = jobs or os.cpu_count() or 1
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
                "--log-level", "warning"]
        if os.geteuid() == 0:
            # distccd won't compile as root; the pid file must be its to write
            argv += ["--user", "distcc"]
            try:
                import pwd
                user = pwd.getpwnam("distcc")
                os.chown(os.path.dirname(self.pid_file), 0, 0)
                open(self.log_file, "a").close()
                os.chown(self.log_file, user.pw_uid, user.pw_gid)
            except (KeyError, OSError) as e:
                raise HelperError("distcc's user isn't set up: %s" % e)
        result = subprocess.run(argv, capture_output=True, text=True)
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
        return {"DISTCC_HOSTS": "127.0.0.1:%d/%d" % (self.port, self.jobs)}

    def pid(self):
        try:
            with open(self.pid_file) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return None
        return pid if _is_distccd(pid) else None

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
            log("== distcc: stopped")
        try:
            os.unlink(self.pid_file)
        except OSError:
            pass


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
