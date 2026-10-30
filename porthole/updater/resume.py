"""
    Porthole: carrying on with installs that stopped part way

    An install job that is cancelled or fails leaves its plan (the root
    plan it carried out, with the whole merge list). What it didn't get to
    becomes an entry in the user's install queue, planned again like any
    other install when the queue is processed. Before that, and whenever
    the queue is shown, packages built since the job started (by the job,
    or by an update since) are dropped, so nothing is built twice.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import getpass
import os

from porthole.updater.service import read_json

VDB = "/var/db/pkg"
# jobs whose installs stopped part way
STOPPED = ("failed", "cancelled")


def built_since(cpv, when):
    """Whether cpv is installed and was built at or after when"""
    try:
        with open(os.path.join(VDB, cpv, "BUILD_TIME")) as f:
            return int(f.read().split()[0]) >= int(when)
    except (OSError, ValueError, IndexError):
        return False


def left(packages, since):
    """The packages (cpvs) not built since the job started"""
    return [cpv for cpv in packages if not built_since(cpv, since)]


def _cp(cpv):
    from portage.versions import cpv_getkey
    return cpv_getkey(cpv)


def _atom_cp(atom):
    from portage.dep import Atom
    try:
        return Atom(atom).cp
    except Exception:
        return atom


def entry(job):
    """An install queue entry carrying on with a stopped install job, or
    None if it left nothing to do (or has no plan to go by)"""
    if job.get("kind") != "install" or job.get("status") not in STOPPED:
        return None
    plan = read_json(job.get("plan_file") or "")
    if not plan or not plan.get("merge_list"):
        return None
    request = plan.get("request") or {}
    started = job.get("started") or job.get("submitted") or 0
    packages = [m["cpv"] for m in plan["merge_list"]]
    remaining = left(packages, started)
    if not remaining:
        return None
    targets = [t for t in request.get("targets") or [] if not t.startswith("--root")]
    options = [t for t in targets if t.startswith("-") and t != "--oneshot"]
    oneshot = "--oneshot" in targets
    # what goes in the world file: the request's "world" (a mixed queue),
    # else everything asked for unless it was one-shot
    asked = request.get("world") or ([] if oneshot else
                                     [a for a in targets if not a.startswith("-")])
    world_cps = {_atom_cp(a) for a in asked}
    title = job.get("title") or "Install"
    return {
        "name": "Unfinished: " + title,
        # everything one-shot; what was asked for normally goes in the
        # world file after (InstallQueue.combined, Analyzer.install_commands)
        "targets": ["--oneshot"] + options + ["=" + cpv for cpv in remaining],
        "requested": list(request.get("requested") or ()),
        "world": ["=" + cpv for cpv in remaining if _cp(cpv) in world_cps],
        "resume": {"job": job["id"], "started": started, "packages": remaining,
                   "total": len(packages)},
    }


def unfinished(jobs, user=None):
    """Queue entries for this user's stopped installs, newest first; only
    the newest of jobs that asked for the same thing, and none for a
    request a later job carried out"""
    user = user or getpass.getuser()
    found, seen = [], set()
    for job in reversed(jobs):
        if job.get("by") != user or job.get("kind") != "install":
            continue
        key = job.get("title")
        if key in seen:
            continue
        seen.add(key)
        if job.get("status") in STOPPED:
            item = entry(job)
            if item:
                found.append((job["id"], item))
    return found


def refresh(item):
    """Drop what was built since; False if nothing is left"""
    info = item.get("resume")
    if not info:
        return True
    remaining = left(info["packages"], info["started"])
    if remaining == info["packages"]:
        return bool(remaining)
    keep = {"=" + cpv for cpv in remaining}
    item["targets"] = [t for t in item["targets"] if t.startswith("-") or t in keep]
    item["world"] = [w for w in item.get("world") or [] if w in keep]
    info["packages"] = remaining
    return bool(remaining)
