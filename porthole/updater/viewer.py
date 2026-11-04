"""
    Porthole update assistant: reading the job queue

    Helpers for the tray and the update assistant to describe the job
    runner's queue (jobs.json). A job's output is shown by following its
    log in Porthole's terminal (follow.py).

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

from porthole.updater.runner import JOBS_FILE
from porthole.updater.service import read_json

STATUS_WORDS = {"queued": "Waiting", "running": "Running", "done": "Finished",
                "failed": "Failed", "cancelled": "Cancelled", "not run": "Not run",
                "fixed": "Failed, fixed by the steps after it"}


def read_jobs():
    return (read_json(JOBS_FILE, {}) or {}).get("jobs", [])


def current_job(jobs=None):
    """The running job, else the next queued one, else the last finished"""
    jobs = read_jobs() if jobs is None else jobs
    for status in ("running", "queued"):
        for job in jobs:
            if job["status"] == status:
                return job
    return jobs[-1] if jobs else None


def describe(job):
    """One line about a job, e.g. for a tooltip"""
    if not job:
        return ""
    progress = job.get("progress") or {}
    if job["status"] == "running" and job.get("paused"):
        return "%s: paused while you use the computer" % job["title"]
    if job["status"] == "queued" and job.get("when") == "window":
        return "%s: waits for the build window%s" % (
            job["title"], " (%s)" % job["waiting"] if job.get("waiting") else "")
    if job["status"] == "running" and job.get("rechecking"):
        return "%s: checking what is left" % job["title"]
    if job["status"] == "running" and progress:
        return "%s: %d of %d done, building %s" % (
            job["title"], progress["done"], progress["total"], progress["current"])
    if job["status"] == "running":
        step = next((s for s in job["steps"] if s["status"] == "running"), None)
        return "%s: %s" % (job["title"], step["title"] if step else "running")
    return "%s: %s" % (job["title"], STATUS_WORDS.get(job["status"], job["status"]).lower())
