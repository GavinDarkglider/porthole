"""
    Porthole update assistant: the build window

    Updates can be approved now and built later, in a window the admin
    sets in updater.conf (build_window = 01:00-06:00), so a long build
    never starts while someone is working. A job waiting for the window
    starts once it is open, the machine is on mains power (if asked) and
    nobody has used it for idle_minutes. If someone comes back during the
    build, it is paused (SIGSTOP) until they leave again.

    The runner is root and can't ask an X session how long it has been
    idle, so the tray (running in the user's session) writes it down
    every half minute in its runtime directory:

        /run/user/UID/porthole-activity   {"time": ..., "idle": seconds}

    With no fresh note from anyone (no tray running), the time window
    alone decides.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import datetime
import glob
import json
import os
import re
import time

ACTIVITY_NAME = "porthole-activity"
# the tray writes every WRITE_EVERY seconds; older notes are ignored
WRITE_EVERY = 30
FRESH = 3 * WRITE_EVERY
# used within this long counts as someone at the machine (pause)
ACTIVE = 60
_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(text):
    """'01:00-06:00' -> (60, 360) minutes after midnight; None if not
    set. May wrap past midnight (22:00-06:00). ValueError if malformed."""
    if not text or not text.strip():
        return None
    match = _WINDOW.match(text)
    if not match:
        raise ValueError("A build window looks like 01:00-06:00")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("Not a time of day: %s" % text.strip())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ValueError("The build window can't start and end at the same time")
    return start, end


def format_window(window):
    return "%02d:%02d-%02d:%02d" % (window[0] // 60, window[0] % 60,
                                    window[1] // 60, window[1] % 60)


def in_window(window, now=None):
    if window is None:
        return False
    t = datetime.datetime.fromtimestamp(now or time.time())
    minute = t.hour * 60 + t.minute
    start, end = window
    return start <= minute < end if start < end else minute >= start or minute < end


def next_start(window, now=None):
    """When the window next opens (a datetime), or now if it is open"""
    now = now or time.time()
    if in_window(window, now):
        return datetime.datetime.fromtimestamp(now)
    t = datetime.datetime.fromtimestamp(now)
    start = t.replace(hour=window[0] // 60, minute=window[0] % 60, second=0, microsecond=0)
    if start <= t:
        start += datetime.timedelta(days=1)
    return start


# -- someone at the machine? ---------------------------------------------------

def x_idle_seconds():
    """How long this X session has had no input, or None (no X, no libXss)"""
    import ctypes
    import ctypes.util

    class Info(ctypes.Structure):
        _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int),
                    ("kind", ctypes.c_int), ("til_or_since", ctypes.c_ulong),
                    ("idle", ctypes.c_ulong), ("event_mask", ctypes.c_ulong)]
    try:
        xlib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
        xss = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xss") or "libXss.so.1")
    except OSError:
        return None
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(Info)
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                          ctypes.POINTER(Info)]
    xlib.XFree.argtypes = [ctypes.c_void_p]
    display = xlib.XOpenDisplay(None)
    if not display:
        return None
    try:
        info = xss.XScreenSaverAllocInfo()
        if not xss.XScreenSaverQueryInfo(display, xlib.XDefaultRootWindow(display), info):
            return None
        idle = info.contents.idle / 1000.0
        xlib.XFree(info)
        return idle
    finally:
        xlib.XCloseDisplay(display)


def activity_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.join(runtime, ACTIVITY_NAME)


def write_activity():
    """The tray's half: note how long this session has been idle"""
    idle = x_idle_seconds()
    if idle is None:
        return False
    path = activity_path()
    try:
        with open(path + ".new", "w") as f:
            json.dump({"time": time.time(), "idle": idle}, f)
        os.replace(path + ".new", path)
    except OSError:
        return False
    return True


def idle_for(now=None):
    """The runner's half: seconds since anyone used the machine (the
    freshest note of every session), or None if nobody's tray says"""
    now = now or time.time()
    last = None
    for path in glob.glob("/run/user/*/" + ACTIVITY_NAME):
        try:
            with open(path) as f:
                note = json.load(f)
            seen = float(note["time"])
            active = seen - float(note["idle"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if now - seen > FRESH:
            continue
        last = active if last is None else max(last, active)
    return None if last is None else max(0.0, now - last)


def can_start(config, now=None):
    """Whether a job waiting for the build window may start now:
    (True, None), or (False, why not)"""
    from porthole.updater.service import on_battery
    window = config.get("build_window")
    if window is None:
        return False, "no build window is set"
    if not in_window(window, now):
        return False, "the build window (%s) is closed" % format_window(window)
    if config.get("window_needs_ac") and on_battery():
        return False, "running on battery"
    wanted = config.get("idle_minutes", 0) * 60
    idle = idle_for(now)
    if wanted and idle is not None and idle < wanted:
        return False, "the computer is in use"
    return True, None


def low_priority_env():
    """Environment for builds that should give way to everything else"""
    return {"PORTAGE_NICENESS": "19",
            "PORTAGE_IONICE_COMMAND": "ionice -c 3 -p ${PID}"}
