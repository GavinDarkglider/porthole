"""
    Porthole crossdev plugin: a system's own settings (its make.conf)

    Porthole made the system's make.conf and nothing else uses it, so the
    settings window writes it directly (this computer's own make.conf is
    never written). Porthole's settings are one marked block:

        # --- Porthole: system settings (changed in Porthole) ---
        CFLAGS="-mcpu=cortex-a57 -O2 -pipe"
        USE="wayland -gtk"
        # --- end of Porthole's system settings ---

    above "# Your own settings below.": lines the user adds there come
    later, so they win, and Porthole leaves them alone.

    Variables portage adds up (USE, VIDEO_CARDS, FEATURES, ...) are
    written as changes to the profile's ("wayland -gtk"); the others
    (CFLAGS, CHOST, MAKEOPTS, ...) as the whole value. The profile's own
    values are what the window starts from.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import re
import subprocess

BEGIN = "# --- Porthole: system settings (changed in Porthole) ---"
END = "# --- end of Porthole's system settings ---"
OWN = "# Your own settings below."

# the whole value is written
COMPILER = ("CHOST", "CFLAGS", "CXXFLAGS", "FCFLAGS", "FFLAGS", "LDFLAGS", "RUSTFLAGS",
            "MAKEOPTS")
# changes to the profile's (portage adds these up)
INCREMENTAL = ("USE", "FEATURES", "ACCEPT_KEYWORDS", "ACCEPT_LICENSE")
HARDWARE = ("VIDEO_CARDS", "INPUT_DEVICES", "CPU_FLAGS_ARM", "CPU_FLAGS_X86", "CPU_FLAGS_PPC",
            "L10N", "ALSA_CARDS")
ORDER = COMPILER + INCREMENTAL + HARDWARE
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")
# what a value may hold: no quotes, no commands; ${VAR} is fine
_VALUE = re.compile(r'^[^"`\\\n\r]*$')
NOTES = {"CHOST": "the system's CPU, operating system and C library",
         "CFLAGS": "C compiler", "CXXFLAGS": "C++ compiler", "FCFLAGS": "Fortran",
         "FFLAGS": "Fortran 77", "LDFLAGS": "linker", "RUSTFLAGS": "Rust",
         "MAKEOPTS": "parallel builds"}
# -march values that build for any CPU of the kind, not the board's
GENERIC = re.compile(r"^(x86-64(-v[1-4])?|i[3-6]86|armv[0-9.]+-?[a-z]?|armv[0-9.]+-a"
                     r"|generic|native|rv64gc|rv32gc|loongarch64|powerpc64(le)?)$")


class ConfError(Exception):
    pass


def incremental(name):
    """Whether portage adds the variable up (profile, then make.conf)"""
    return name in INCREMENTAL or name in HARDWARE or (
        name not in COMPILER and name.startswith(("CPU_FLAGS_", "VIDEO_", "INPUT_")))


def stack(words, start=()):
    """The words an incremental variable ends with: -* clears, -x drops x"""
    result = list(start)
    for word in words:
        if word == "-*":
            result = []
        elif word.startswith("-"):
            result = [w for w in result if w != word[1:]]
        elif word not in result:
            result.append(word)
    return result


def check_value(name, value):
    if not _NAME.match(name or ""):
        raise ConfError("Not a variable name: %r" % name)
    if value is not None and not _VALUE.match(value):
        raise ConfError("%s can't hold quotes, backslashes, ` or line breaks" % name)


# -- reading ----------------------------------------------------------------------

def split_text(text):
    """(before, Porthole's block lines, after) of a make.conf"""
    if BEGIN in text and END in text.split(BEGIN, 1)[1]:
        before, rest = text.split(BEGIN, 1)
        block, after = rest.split(END, 1)
        return before, block, after
    return text, "", ""


def _assignments(text):
    """{VAR: [words...]} of a make.conf text, in order; for a variable
    assigned more than once the last value (which already holds ${VAR}
    for the earlier ones)"""
    from porthole.updater.system import _statements, _value, _ASSIGN
    found = {}
    for _comments, statement in _statements(text):
        name = _ASSIGN.match(statement).group(1)
        words = _value(statement, references=True)
        own_ref = {"$" + name, "${%s}" % name}
        if incremental(name):
            found.setdefault(name, [])
            found[name] += [w for w in words if w not in own_ref]
        else:
            found[name] = words
    return found


def read_text(target):
    try:
        with open(os.path.join(target.portage_dir, "make.conf")) as f:
            return f.read()
    except OSError:
        return ""


def profile_values(target):
    """{VAR: value} the system's profile sets (before make.conf), and the
    names of its USE_EXPAND variables, from portage"""
    import portage
    try:
        settings = portage.config(config_root=target.config_root, target_root=target.root,
                                  sysroot=target.sysroot, eprefix="")
    except Exception as e:
        raise ConfError("Portage can't read %s's settings: %s" % (target.name, e))
    defaults = settings.configdict["defaults"]
    values = {}
    for name in set(ORDER) | set(defaults.keys()):
        raw = defaults.get(name)
        if raw is None:
            continue
        values[name] = " ".join(stack(raw.split())) if incremental(name) else raw
    expand = [n for n in defaults.get("USE_EXPAND", "").split()]
    hidden = set(defaults.get("USE_EXPAND_HIDDEN", "").split())
    return values, expand, hidden, [r.location for r in settings.repositories], settings


def read(target):
    """Everything the settings window shows, from the system's profile and
    make.conf"""
    profile, expand, hidden, locations, settings = profile_values(target)
    text = read_text(target)
    before, block, after = split_text(text)
    porthole = {name: " ".join(words) for name, words in _assignments(block).items()}
    own = _assignments(before + after)
    return {"profile": profile, "porthole": porthole,
            "own": {name: " ".join(words) for name, words in own.items()},
            "arch": settings.get("ARCH", ""), "expand": expand, "hidden": sorted(hidden),
            "locations": locations, "has_block": bool(block or BEGIN in text)}


def effective(info, name, porthole=None):
    """A variable's value with the profile, Porthole's block (or the given
    one) and the user's own lines"""
    porthole = info["porthole"] if porthole is None else porthole
    profile, mine, own = (info["profile"].get(name), porthole.get(name),
                          info["own"].get(name))
    if incremental(name):
        words = stack((profile or "").split())
        for value in (mine, own):
            if value:
                words = stack(value.split(), words)
        return " ".join(words)
    for value in (own, mine, profile):
        if value is not None:
            return value
    return ""


# -- writing ----------------------------------------------------------------------

def render_block(values):
    lines = [BEGIN]
    names = [n for n in ORDER if n in values] + sorted(n for n in values if n not in ORDER)
    for name in names:
        value = values[name]
        if value is None:
            continue
        check_value(name, value)
        note = NOTES.get(name)
        if note:
            lines.append("# " + note)
        lines.append('%s="%s"' % (name, " ".join(value.split())))
    lines.append(END)
    return "\n".join(lines)


def set_text(text, values):
    """make.conf text with Porthole's block holding exactly values"""
    block = render_block(values)
    before, old, after = split_text(text)
    if BEGIN in text:
        return before + block + after
    if OWN in text:
        head, tail = text.split(OWN, 1)
        return head.rstrip("\n") + "\n\n" + block + "\n\n" + OWN + tail
    return text.rstrip("\n") + "\n\n" + block + "\n"


def write(target, values):
    """Porthole's block in the system's make.conf: {VAR: value or None}
    (None: not set by Porthole)"""
    values = {k: v for k, v in values.items() if v is not None}
    for name, value in values.items():
        check_value(name, value)
    path = os.path.join(target.portage_dir, "make.conf")
    text = read_text(target)
    _write_file(path, set_text(text, values))


def _write_file(path, text):
    with open(path + ".new", "w") as f:
        f.write(text)
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)


# -- the compiler -----------------------------------------------------------------

def cpu_option(chost):
    """The gcc option naming the CPU to build for: -mcpu on Arm, -march elsewhere"""
    return "-mcpu" if chost.startswith(("aarch64", "arm")) else "-march"


def cpus(chost, host_chost=None):
    """[cpu names] the system's gcc can tune for (asked from the cross gcc,
    or this computer's own when the system is for the same CHOST), or None
    when there is no such gcc yet"""
    import shutil
    gcc = shutil.which(chost + "-gcc") or (shutil.which("gcc") if chost == host_chost else None)
    if not gcc:
        return None
    option = cpu_option(chost)
    try:
        result = subprocess.run([gcc, option + "=porthole-none", "-x", "c", "-c", "-o",
                                 os.devnull, "-"], input="", capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"valid arguments (?:to '[^']*' )?are: ([^\n;]*)", result.stderr)
    if not match:
        return None
    return [w for w in match.group(1).split() if w not in ("native",)]


def set_cpu(flags, chost, cpu):
    """CFLAGS with the CPU options replaced by one for cpu"""
    words = [w for w in flags.split()
             if not w.startswith(("-mcpu=", "-march=", "-mtune="))]
    return " ".join(["%s=%s" % (cpu_option(chost), cpu)] + words)


def tuning(flags):
    """(what the flags build for, problem or None): flags must name the
    system's CPU, not any CPU of the kind (generic), nor this computer's
    (native)"""
    words = flags.split()
    found = {}
    for word in words:
        for option in ("-mcpu", "-march", "-mtune"):
            if word.startswith(option + "="):
                found[option] = word.split("=", 1)[1]
    if "native" in found.values():
        return None, ("native means the CPU of the computer doing the build, not the "
                      "system's: name its CPU instead")
    if found.get("-mcpu") and found["-mcpu"] != "generic":
        return found["-mcpu"], None
    if found.get("-mtune") and found["-mtune"] != "generic":
        return found["-mtune"], None
    march = found.get("-march")
    if march and not GENERIC.match(march.split("+", 1)[0]):
        return march, None
    return None, ("doesn't name the system's CPU (%s): packages would be built for any "
                  "CPU of its kind" % ("only " + march if march else "no -mcpu or -march"))


def checklist(info, values=None):
    """[(tab, done, text)] of what to look at before building"""
    def get(name):
        return effective(info, name, values)
    items = []
    cpu, problem = tuning(get("CFLAGS"))
    items.append(("compiler", problem is None,
                  "Compiler flags name the CPU (%s)" % cpu if cpu else
                  "Compiler flags: " + problem))
    cxx = get("CXXFLAGS")
    if cxx and cxx not in ("${CFLAGS}", "$CFLAGS") and cxx != get("CFLAGS"):
        items.append(("compiler", tuning(cxx)[1] is None,
                      "C++ flags differ from the C flags; check they name the CPU too"))
    items.append(("compiler", bool(get("CHOST")), "CHOST: %s" % (get("CHOST") or "not set")))
    items.append(("compiler", bool(get("MAKEOPTS")),
                  "Parallel builds (MAKEOPTS): %s" % (get("MAKEOPTS") or
                                                      "not set, builds one file at a time")))
    for name, what in (("VIDEO_CARDS", "graphics drivers"), ("INPUT_DEVICES", "input drivers")):
        value = get(name)
        items.append(("hardware", bool(value), "%s (%s): %s" % (
            name, what, value or "none: no %s are built" % what)))
    cpu_flags = "CPU_FLAGS_X86" if info["arch"] in ("amd64", "x86") else (
        "CPU_FLAGS_ARM" if info["arch"] in ("arm", "arm64") else None)
    if cpu_flags:
        items.append(("hardware", bool(get(cpu_flags)), "%s: %s" % (
            cpu_flags, get(cpu_flags) or "none: packages won't use the CPU's extra "
            "instructions")))
    return items


# -- the system's other Portage files ----------------------------------------------

FILE_DIRS = ("package.use", "package.accept_keywords", "package.mask", "package.unmask",
             "package.license", "package.env", "env")
_FILE = re.compile(r"^(make\.conf|(%s)(/[A-Za-z0-9][A-Za-z0-9._+-]{0,80})?)$"
                   % "|".join(re.escape(d) for d in FILE_DIRS))
MAX_FILE = 96 * 1024


def files(target):
    """[relative path] of the files the Files tab can open: make.conf, and
    what is in package.* and env (a single package.use file, or the files
    of a package.use folder)"""
    found = ["make.conf"]
    for name in FILE_DIRS:
        path = os.path.join(target.portage_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            found += ["%s/%s" % (name, f) for f in sorted(os.listdir(path))
                      if _FILE.match("%s/%s" % (name, f))
                      and os.path.isfile(os.path.join(path, f))]
        elif os.path.isfile(path):
            found.append(name)
    return found


def file_path(target, relative):
    """The full path of one of the system's Portage files, checked: only
    those files, inside the system's etc/portage, never through a link"""
    if not _FILE.match(relative or ""):
        raise ConfError("Porthole doesn't edit %s" % relative)
    base = os.path.realpath(target.portage_dir)
    path = os.path.join(base, relative)
    parent = os.path.dirname(path)
    for p in (path, parent):
        if os.path.islink(p):
            raise ConfError("%s is a link; Porthole won't write through it" % relative)
    if os.path.dirname(relative):
        top = os.path.join(base, os.path.dirname(relative))
        if os.path.isfile(top):
            raise ConfError("%s is a single file, not a folder" % os.path.dirname(relative))
    if not os.path.realpath(parent).startswith(base):
        raise ConfError("%s isn't in the system's settings" % relative)
    return path


def write_file(target, relative, text):
    if len(text.encode()) > MAX_FILE:
        raise ConfError("That's too big for a settings file")
    path = file_path(target, relative)
    if relative != "make.conf" and os.path.isdir(path):
        raise ConfError("%s is a folder: give a file in it" % relative)
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    if text.strip() == "" and relative != "make.conf":
        if os.path.exists(path):
            os.unlink(path)
        return "Deleted %s" % relative
    if not text.endswith("\n"):
        text += "\n"
    _write_file(path, text)
    return "Saved %s" % relative
