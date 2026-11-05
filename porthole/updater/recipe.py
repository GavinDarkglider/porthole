#!/usr/bin/env python3

'''
    Porthole: a job's command record

    Next to each job's log (<job>.log) Porthole writes <job>.sh: every
    command the job ran, with the settings it ran them with, in the order
    they ran, as a bash script with notes on what each step does. It is
    for reading (how Gentoo builds a system, step by step), and the same
    commands could be typed by hand; it ends with what each step took and
    an estimate of the time a person would spend doing it all by hand.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
'''

import os
import re
import shlex
import time

# the record being written by the job running in this process, if any
_current = None

# settings whose values stay out of the record
SECRET = re.compile(r"PASS|SECRET|TOKEN|AUTH|KEY|CREDENTIAL", re.I)

# the estimate of doing it by hand (minutes; shown in the record with the sums)
HAND_SETUP = 120     # writing the settings of the "Settings" parts by hand, for
                     # someone who already knows Gentoo
HAND_FIX = 45        # reading the log, finding the cause and the fix, for each
                     # problem Porthole fixed by itself
HAND_COMMAND = 3     # typing each command, waiting for it and checking it worked
PORTHOLE_HANDS = 5   # choosing the settings in Porthole and pressing Start

# files larger than this are noted, not copied into the record
LARGEST = 64 * 1024


def current():
    """The record of the job running now, or None"""
    return _current


def ran(argv, status=0, env=None, seconds=None):
    """Note a command a job ran outside run_logged (mounts, services)"""
    if _current is not None:
        _current.command(argv, env, status, seconds)


def note(text, once=None):
    if _current is not None:
        _current.note(text, once)


def record_path(log_path):
    return (log_path[:-4] if log_path.endswith(".log") else log_path) + ".sh"


def _duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%d s" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%d min" % minutes
    return "%d h %02d min" % (minutes // 60, minutes % 60)


def _comment(text, indent=""):
    return "".join("%s# %s\n" % (indent, line) if line else "%s#\n" % indent
                   for line in text.strip("\n").split("\n"))


class Recipe:
    """The record of one job, written as the job goes (it is all there
    even if the job stops part way)"""

    def __init__(self, path, job):
        self.path = path
        self.env = {}           # the settings exported so far
        self.explained = set()  # notes already given
        self.commands = 0
        self.file = open(path, "w")
        os.chmod(path, 0o644)
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.get("started") or time.time()))
        self._write("#!/bin/bash\n" + _comment(
            "%s\n"
            "Job %s, asked for by %s, started %s\n"
            "\n"
            "What Porthole ran for this job, in order, with the settings it ran each\n"
            "command with: a record to read and learn from. The notes say what each\n"
            "step does and why. Commands that failed are marked, and the steps\n"
            "Porthole added to fix them follow. The end of the file has what each\n"
            "step took and an estimate of doing it all by hand.\n"
            "\n"
            "Running it repeats the job as it went here, failures and fixes included;\n"
            "it has to be run as root, on this computer, with --run."
            % (job.get("title", "Job"), job.get("id", "?"), job.get("by", "?"), started))
            + '\n[[ ${1} == --run ]] || { echo "A record to read: see the notes inside '
              '(run it with --run to repeat it)."; exit 1; }\n')

    def _write(self, text):
        self.file.write(text)
        self.file.flush()

    def close(self):
        if not self.file.closed:
            self.file.close()

    # -- what the job does ---------------------------------------------------

    def section(self, title, note=None):
        self._write("\n\n# %s\n# %s\n" % ("=" * 74, title))
        if note:
            self.note(note)
        self._write("# %s\n" % ("=" * 74))

    def note(self, text, once=None):
        """A note; once: a key, so it is only given the first time"""
        if once is not None:
            if once in self.explained:
                return
            self.explained.add(once)
        self._write(_comment(text))

    def heading(self, text):
        self._write("\n# --- %s\n" % text)

    def _settings(self, env):
        """export/unset lines for what changed since the last command"""
        lines = []
        for key in sorted(env):
            value = "(hidden)" if SECRET.search(key) else env[key]
            if self.env.get(key) != value:
                lines.append("export %s=%s" % (key, shlex.quote(value)))
                self.env[key] = value
        return lines

    def command(self, argv, env=None, status=0, seconds=None):
        self.commands += 1
        lines = self._settings(env or {})
        line = shlex.join(argv)
        outcome = ""
        if status:
            outcome = "   # failed (exit status %d)" % status
        if seconds is not None and seconds >= 60:
            outcome += "%s took %s" % ("," if outcome else "   #", _duration(seconds))
        self._write("\n".join(lines + [line + outcome]) + "\n")

    def file_contents(self, path, shown=None):
        """A file as it is now, as the commands that write it (shown: the
        path written in the record, when it differs)"""
        shown = shown or path
        if os.path.islink(path):
            self._write("ln -sfn %s %s\n" % (shlex.quote(os.readlink(path)), shlex.quote(shown)))
            return
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                data = f.read(LARGEST + 1)
        except OSError:
            return
        if size > LARGEST or b"\0" in data:
            self._write("# %s: %s, not copied here\n" % (
                shown, "a binary file" if b"\0" in data else "%d bytes" % size))
            return
        text = data.decode(errors="replace")
        end = "PORTHOLE_EOF"
        while end in text:
            end += "_"
        if text and not text.endswith("\n"):
            text += "\n"
        mode = os.stat(path).st_mode & 0o7777
        self._write("mkdir -p %s\ncat > %s <<'%s'\n%s%s\n" % (
            shlex.quote(os.path.dirname(shown)), shlex.quote(shown), end, text, end))
        if mode & 0o111:
            self._write("chmod %o %s\n" % (mode, shlex.quote(shown)))

    def folder(self, top, skip=lambda path: False):
        """Every file under top (sorted, links as links)"""
        if os.path.islink(top) or os.path.isfile(top):
            self.file_contents(top)
            return
        for folder, dirs, files in os.walk(top):
            dirs.sort()
            for name in sorted(files) + [d for d in dirs
                                         if os.path.islink(os.path.join(folder, d))]:
                path = os.path.join(folder, name)
                if not skip(path):
                    self.file_contents(path)

    # -- the end ---------------------------------------------------------------

    def finish(self, job):
        """What each step took, and the estimate of doing it by hand"""
        steps = job.get("steps") or []
        lines = ["", "%s" % ("=" * 74), "How it went: %s" % {
            "done": "finished", "failed": "failed", "cancelled": "cancelled"}.get(
                job.get("status"), job.get("status", "?")), ""]
        for number, step in enumerate(steps, 1):
            took = (step["finished"] - step["started"]) if step.get("finished") is not None \
                and step.get("started") is not None else None
            lines.append("%3d. %-52s %-10s %s" % (
                number, step.get("title", "?")[:52], step.get("status", "?"),
                _duration(took) if took is not None else ""))
        now = time.time()
        total = (now if job.get("finished") is None else job["finished"]) - (
            now if job.get("started") is None else job["started"])
        fixed = sum(1 for s in steps if s.get("status") == "fixed")
        added = sum(1 for s in steps if s.get("added"))
        by_hand = HAND_SETUP + fixed * HAND_FIX + self.commands * HAND_COMMAND
        lines += [
            "",
            "Time from start to end: %s. The computer does the same work either way." % (
                _duration(total)),
            "",
            "Estimate of the time a person would spend doing this by hand (minutes):"]
        row = lambda label, minutes, why="": "  %-40s %5d   %s" % (label, minutes, why)
        lines += [
            row("writing the settings above", HAND_SETUP),
            row("finding and fixing %d problem%s" % (fixed, "" if fixed == 1 else "s"),
                fixed * HAND_FIX, "(%d min each)" % HAND_FIX),
            row("typing and checking %d command%s" % (self.commands,
                                                       "" if self.commands == 1 else "s"),
                self.commands * HAND_COMMAND, "(%d min each)" % HAND_COMMAND),
            row("by hand, in all", by_hand, "(%s)" % _duration(by_hand * 60)),
            row("with Porthole", PORTHOLE_HANDS, "(choosing settings, Start)"),
            row("saved", by_hand - PORTHOLE_HANDS,
                "(%s)" % _duration((by_hand - PORTHOLE_HANDS) * 60)),
            ""]
        if fixed:
            lines += [
                "Porthole added %d step%s to fix %d problem%s and carried on. Run by hand"
                % (added, "" if added == 1 else "s", fixed, "" if fixed == 1 else "s"),
                "with nobody watching, each of those would have stopped the build until",
                "someone came back to it.", ""]
        lines += [
            "The minutes per item are a rough guide (the HAND_* values in Porthole's",
            "updater/recipe.py); someone new to Gentoo would take longer, an expert",
            "with their own scripts less."]
        self._write(_comment("\n".join(lines)))


def start(log_path, job):
    """Begin the record of the job running now (None when it can't be written)"""
    global _current
    try:
        _current = Recipe(record_path(log_path), job)
    except OSError:
        _current = None
    return _current


def finish(job):
    global _current
    if _current is not None:
        try:
            _current.finish(job)
        finally:
            _current.close()
            _current = None
