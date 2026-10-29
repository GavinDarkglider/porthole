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
import sys
import time

from porthole.updater.runner import ACTIVE, JOBS_FILE
from porthole.updater.service import read_json

# start this far from the end of a long log (emerge build output is big)
REPLAY_BYTES = 8 * 1024 * 1024


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


def show_in_terminal(job_id=None, on_close=None):
    """Follow a job in a Porthole terminal window of its own (Porthole's
    preferences must be loaded). Returns the ProcessManager."""
    from porthole.terminal.terminal import ProcessManager
    from porthole.utils import utils
    job = find(job_id)
    manager = ProcessManager(utils.environment())
    manager.add(job["title"] if job else "Update progress", command(job_id),
                lambda: None, "Update Assistant")
    if on_close:
        manager.window.connect("destroy", lambda w: on_close())
    return manager


if __name__ == "__main__":
    sys.exit(follow(sys.argv[1] if len(sys.argv) > 1 else None))
