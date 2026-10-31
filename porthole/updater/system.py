"""
    Porthole: a system's Portage settings, as the user wrote them

    Reads make.conf the way its author laid it out (each USE line with the
    comment above it as a group), what the profile adds, what each flag
    means, and facts about the machine to set the build options against.
    Everything here only reads; changes are made elsewhere, as root.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import re
import shlex
import subprocess

_ASSIGN = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', re.S)
# USE_EXPAND variables people set in make.conf, with their descriptions file
EXPAND = ("VIDEO_CARDS", "INPUT_DEVICES", "CPU_FLAGS_X86", "L10N", "PYTHON_TARGETS",
          "LLVM_TARGETS", "ABI_X86", "GRUB_PLATFORMS", "LUA_TARGETS", "RUBY_TARGETS")


def make_conf_files(portage_dir="/etc/portage"):
    """make.conf, or the files of a make.conf directory, in the order read"""
    path = os.path.join(portage_dir, "make.conf")
    if os.path.isdir(path):
        return [os.path.join(path, f) for f in sorted(os.listdir(path))
                if not f.startswith(".") and os.path.isfile(os.path.join(path, f))]
    return [path] if os.path.exists(path) else []


def _statements(text):
    """(comment lines above, assignment text) for each assignment, with
    backslash-continued lines joined"""
    comments, pending, out = [], None, []
    for raw in text.splitlines():
        line = raw.rstrip()
        if pending is not None:
            pending += " " + line.rstrip("\\").strip()
            if not line.endswith("\\") and pending.count('"') % 2 == 0:
                out.append((comments, pending))
                comments, pending = [], None
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
        elif not stripped:
            comments = [] if not comments or comments[-1] else comments
            if comments:
                comments = []
        elif _ASSIGN.match(stripped):
            if stripped.endswith("\\") or stripped.count('"') % 2:
                pending = stripped.rstrip("\\").strip()
            else:
                out.append((comments, stripped))
                comments = []
        else:
            comments = []
    return out


def _value(text, references=False):
    """The words of an assignment's value, without quotes, and without
    $VAR / ${VAR} unless references (USE="${USE} ..." adds to USE; for
    CFLAGS="${COMMON_FLAGS}" the reference is the value)"""
    match = _ASSIGN.match(text)
    try:
        words = shlex.split(match.group(2), comments=True)
    except ValueError:
        words = match.group(2).strip('"\'').split()
    value = " ".join(words).split()
    if references:
        return value
    return [w for w in value if not re.fullmatch(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", w)]


def read_make_conf(portage_dir="/etc/portage"):
    """{"use_groups": [{"title", "notes", "flags", "file"}], "vars": {VAR:
    {"text", "notes", "file"}}} as written (last assignment of a variable
    wins; USE lines all add up, as `USE="${USE} ..."` does)"""
    groups, variables = [], {}
    for path in make_conf_files(portage_dir):
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for comments, statement in _statements(text):
            name = _ASSIGN.match(statement).group(1)
            notes = [c for c in comments if c]
            if name == "USE":
                groups.append({"title": notes[0] if notes else "USE flags",
                               "notes": notes[1:], "flags": _value(statement),
                               "file": path})
            else:
                variables[name] = {"text": " ".join(_value(statement, references=True)),
                                   "notes": notes, "file": path}
    return {"use_groups": groups, "vars": variables}


# -- what flags mean ---------------------------------------------------------------

def _repo_profiles():
    try:
        import portage
        return [os.path.join(r.location, "profiles")
                for r in portage.settings.repositories]
    except Exception:
        return ["/var/db/repos/gentoo/profiles"]


def flag_descriptions():
    """{flag: description} for global flags, and {flag: [cat/pkg...]} for
    flags only some packages use (use.local.desc)"""
    descriptions, local = {}, {}
    for profiles in _repo_profiles():
        for line in _lines(os.path.join(profiles, "use.desc")):
            flag, sep, text = line.partition(" - ")
            if sep:
                descriptions.setdefault(flag.strip(), text.strip())
        for line in _lines(os.path.join(profiles, "use.local.desc")):
            head, sep, text = line.partition(" - ")
            pkg, colon, flag = head.partition(":")
            if sep and colon:
                local.setdefault(flag.strip(), []).append(pkg.strip())
                descriptions.setdefault(flag.strip(), text.strip() + " (%s)" % pkg.strip())
    return descriptions, local


def global_flags():
    """Flags Gentoo describes as global (use.desc): many packages use them"""
    flags = set()
    for profiles in _repo_profiles():
        for line in _lines(os.path.join(profiles, "use.desc")):
            flag, sep, _text = line.partition(" - ")
            if sep:
                flags.add(flag.strip())
    return flags


def all_iuse():
    """Every flag some package in the repositories has (its IUSE): from
    the metadata cache where a repository has one, else from the
    ebuilds. Description files aren't enough: overlays often don't
    describe their flags."""
    import glob
    flags = set()
    try:
        import portage
        repos = [r.location for r in portage.settings.repositories]
    except Exception:
        repos = ["/var/db/repos/gentoo"]
    iuse_line = re.compile(r"^IUSE=(.*)$", re.M)
    iuse_var = re.compile(r'IUSE\+?="([^"]*)"')
    for location in repos:
        cache = os.path.join(location, "metadata", "md5-cache")
        if os.path.isdir(cache):
            paths, pattern = glob.glob(cache + "/*/*"), iuse_line
        else:
            paths, pattern = glob.glob(location + "/*/*/*.ebuild"), iuse_var
        for path in paths:
            try:
                with open(path, errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for match in pattern.finditer(text):
                flags.update(w.lstrip("+-") for w in match.group(1).split())
    return flags


def expand_values(name):
    """{value: description} a USE_EXPAND variable may take"""
    values = {}
    for profiles in _repo_profiles():
        for line in _lines(os.path.join(profiles, "desc", name.lower() + ".desc")):
            value, sep, text = line.partition(" - ")
            if sep:
                values.setdefault(value.strip(), text.strip())
    return values


def _lines(path):
    try:
        with open(path, errors="replace") as f:
            return [l.rstrip("\n") for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return []


# -- the profile -----------------------------------------------------------------

def profile(config_root="/"):
    """(name, path) of the profile, e.g. default/linux/amd64/23.0/desktop"""
    link = os.path.join(config_root, "etc", "portage", "make.profile")
    try:
        target = os.path.realpath(link)
    except OSError:
        return None, None
    marker = os.sep + "profiles" + os.sep
    return (target.split(marker, 1)[1] if marker in target else target), target


def profile_use():
    """USE the profile turns on (its make.defaults), before make.conf"""
    try:
        import portage
        return set(portage.settings.configdict["defaults"].get("USE", "").split())
    except Exception:
        return set()


# -- the machine -------------------------------------------------------------------

def machine():
    """Facts to set build options against"""
    facts = {"threads": os.cpu_count() or 1, "memory_gb": None, "cpu": None,
             "cpu_flags": None, "march_native": None}
    try:
        with open("/proc/meminfo") as f:
            kb = int(next(l for l in f if l.startswith("MemTotal")).split()[1])
        facts["memory_gb"] = round(kb / 1024 / 1024, 1)
    except (OSError, StopIteration, ValueError):
        pass
    try:
        with open("/proc/cpuinfo") as f:
            facts["cpu"] = next(l.split(":", 1)[1].strip() for l in f
                                if l.startswith("model name"))
    except (OSError, StopIteration):
        pass
    for key, argv in (("cpu_flags", ["cpuid2cpuflags"]),
                      ("march_native", ["gcc", "-march=native", "-Q", "--help=target"])):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if key == "cpu_flags":
            facts[key] = out.split(":", 1)[1].split() if ":" in out else None
        else:
            match = re.search(r"^\s*-march=\s+(\S+)", out, re.M)
            facts[key] = match.group(1) if match else None
    return facts


def jobs_advice(makeopts, facts):
    """(jobs set, jobs suggested, why) for MAKEOPTS -jN"""
    match = re.search(r"(?:^|\s)(?:-j\s*|--jobs[= ])(\d+)", makeopts or "")
    jobs = int(match.group(1)) if match else None
    threads = facts["threads"]
    memory = facts["memory_gb"]
    by_memory = max(1, int(memory // 2)) if memory else threads
    suggested = min(threads, by_memory)
    why = ("%d thread%s, %s GB of memory (a big C++ compile can take 2 GB per job)"
           % (threads, "" if threads == 1 else "s", memory if memory else "?"))
    return jobs, suggested, why


def findings(conf, facts, iuse):
    """Things worth a look, as (title, detail) pairs: flags no package
    has (iuse: all_iuse()), flags set twice, CPU flags that differ from
    the CPU's, more jobs than the machine handles well"""
    out = []
    seen, twice = {}, []
    known = iuse
    for group in conf["use_groups"]:
        for flag in group["flags"]:
            name = flag.lstrip("-+")
            if name in seen and seen[name] == flag:
                twice.append(name)
            seen[name] = flag
    unknown = sorted(n for n in seen if n not in known and n != "*")
    if unknown:
        out.append(("Flags no package uses",
                    "%s: no package in your repositories has these flags, so setting "
                    "them changes nothing (often left over from packages that changed "
                    "or were removed)." % " ".join(unknown)))
    if twice:
        out.append(("Flags set twice", "%s appear%s more than once with the same "
                    "setting; harmless." % (" ".join(sorted(set(twice))),
                                            "s" if len(set(twice)) == 1 else "")))
    set_flags = conf["vars"].get("CPU_FLAGS_X86", {}).get("text", "").split()
    if facts.get("cpu_flags") is not None and set_flags:
        valid = expand_values("CPU_FLAGS_X86")
        missing = [f for f in facts["cpu_flags"] if f not in set_flags]
        extra = [f for f in set_flags if f not in facts["cpu_flags"]]
        invalid = [f for f in extra if valid and f not in valid]
        if missing or extra:
            parts = []
            if missing:
                parts.append("your CPU also has %s (not set, so packages won't use "
                             "them)" % " ".join(missing))
            if invalid:
                parts.append("%s %s not a CPU flag Gentoo knows" % (
                    " ".join(invalid), "is" if len(invalid) == 1 else "are"))
            elif extra:
                parts.append("%s set but your CPU doesn't have %s (builds may crash)"
                             % (" ".join(extra), "it" if len(extra) == 1 else "them"))
            out.append(("CPU_FLAGS_X86 differs from what cpuid2cpuflags finds",
                        "; ".join(parts) + "."))
    makeopts = conf["vars"].get("MAKEOPTS", {}).get("text")
    jobs, suggested, why = jobs_advice(makeopts, facts)
    if jobs and jobs > suggested:
        out.append(("MAKEOPTS runs more jobs than this machine handles well",
                    "-j%d, but %s suggests -j%d. Too many jobs can run out of memory "
                    "on big packages." % (jobs, why, suggested)))
    return out


# -- Porthole's own global USE (package.use/00-porthole-global) -------------------
#
# Changes made in Settings > System never touch make.conf: they go in one
# file of Porthole's own, "*/* flags" lines, read after make.conf (so they
# win) and before the user's own package.use files (so per-package lines
# still win over them). Deleting the file undoes them all.

GLOBAL_USE_FILE = "00-porthole-global"
_FLAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_@.-]*$")
_EXPAND_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")


def global_use_path(portage_dir="/etc/portage"):
    return os.path.join(portage_dir, "package.use", GLOBAL_USE_FILE)


def read_global_use(portage_dir="/etc/portage"):
    """{"use": {flag: True|False}, "expand": {VAR: [values]}}"""
    model = {"use": {}, "expand": {}}
    for line in _lines(global_use_path(portage_dir)):
        words = line.split()
        if not words or words[0] != "*/*":
            continue
        words = words[1:]
        if words and words[0].endswith(":") and words[0][:-1] in EXPAND:
            model["expand"][words[0][:-1]] = words[1:]
            continue
        for word in words:
            model["use"][word.lstrip("-")] = not word.startswith("-")
    return model


def check_global_use(model):
    """The model, checked (ValueError if not something to write)"""
    if not isinstance(model, dict):
        raise ValueError("Not a set of USE changes")
    use, expand = model.get("use") or {}, model.get("expand") or {}
    if not isinstance(use, dict) or not isinstance(expand, dict):
        raise ValueError("Not a set of USE changes")
    for flag, on in use.items():
        if not isinstance(flag, str) or not _FLAG.match(flag) or not isinstance(on, bool):
            raise ValueError("Not a USE flag setting: %r" % (flag,))
    for name, values in expand.items():
        if name not in EXPAND or not isinstance(values, list) or not all(
                isinstance(v, str) and _EXPAND_VALUE.match(v) for v in values):
            raise ValueError("Not a setting Porthole can make: %r" % (name,))
    return {"use": dict(use), "expand": {k: list(v) for k, v in expand.items()}}


def render_global_use(model):
    """The file's text for a model, or None if there is nothing to set"""
    model = check_global_use(model)
    lines = []
    if model["use"]:
        lines.append("*/* " + " ".join(
            (f if on else "-" + f) for f, on in sorted(model["use"].items())))
    for name, values in sorted(model["expand"].items()):
        lines.append("*/* %s: %s" % (name, " ".join(values)))
    if not lines:
        return None
    return ("# USE changes for every package, made in Porthole (Settings, System).\n"
            "# Read after make.conf, so these win over it; your own package.use\n"
            "# lines for single packages still win over these. Porthole replaces\n"
            "# this file; delete it to undo all of them.\n" + "\n".join(lines) + "\n")


def preview_global_use(model, log=lambda m: None):
    """What changing Porthole's global USE to model would rebuild, worked
    out on a scratch copy of the config (as the user; nothing real is
    changed): the plan"""
    from porthole.updater.analyze import Analyzer, ScratchConfig
    text = render_global_use(model)
    with ScratchConfig("/") as scratch:
        path = global_use_path(scratch.portage_dir)
        directory = os.path.dirname(path)
        if os.path.isfile(directory):
            raise ValueError("%s is a single file, not a directory"
                             % "/etc/portage/package.use")
        os.makedirs(directory, exist_ok=True)
        if text is None:
            if os.path.exists(path):
                os.unlink(path)
        else:
            with open(path, "w") as f:
                f.write(text)
        return Analyzer(config_root=scratch.root, log=log).analyze()


# -- switching profiles --------------------------------------------------------------

def profiles(arch=None):
    """[(name, status, profiles dir)] for this arch from every repository's
    profiles.desc, stable first"""
    try:
        import portage
        arch = arch or portage.settings["ARCH"]
        repos = list(portage.settings.repositories)
    except Exception:
        return []
    found = []
    for repo in repos:
        directory = os.path.join(repo.location, "profiles")
        for line in _lines(os.path.join(directory, "profiles.desc")):
            words = line.split()
            if len(words) >= 3 and words[0] == arch:
                found.append((words[1], words[2], directory))
    order = {"stable": 0, "dev": 1, "exp": 2}
    return sorted(found, key=lambda p: (order.get(p[1], 3), p[0]))


def init_system(name):
    return "systemd" if "systemd" in (name or "").split("/") else "openrc"


def link_profile(portage_dir, profiles_dir, name):
    """Point make.profile at a profile, relative like eselect does"""
    link = os.path.join(portage_dir, "make.profile")
    target = os.path.join(profiles_dir, name)
    if not os.path.isdir(target):
        raise ValueError("No such profile directory: %s" % target)
    relative = os.path.relpath(target, os.path.realpath(portage_dir))
    tmp = link + ".porthole"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(relative, tmp)
    os.replace(tmp, link)


def preview_profile(name, log=lambda m: None):
    """What switching to profile name would rebuild, worked out on a
    scratch copy of the config: the plan"""
    from porthole.updater.analyze import Analyzer, ScratchConfig
    match = [p for p in profiles() if p[0] == name]
    if not match:
        raise ValueError("Not a profile for this computer: %s" % name)
    with ScratchConfig("/") as scratch:
        link = os.path.join(scratch.portage_dir, "make.profile")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.join(match[0][2], name), link)
        return Analyzer(config_root=scratch.root, log=log).analyze()


# -- categories of USE flags ---------------------------------------------------------
#
# Gentoo doesn't categorise USE flags (use.desc is one flat list), so these
# are Porthole's own: common global flags by hand, the rest by words in
# their description, flags only some packages have on their own.

CATEGORIES = (
    ("Desktop and toolkits", """X wayland gtk gtk2 gtk3 gtk4 qt5 qt6 qt4 qt kde gnome xfce
        lxde lxqt plasma kwallet gnome-keyring gnome-online-accounts libnotify
        startup-notification xinerama xcomposite appindicator accessibility gui widgets
        xcb branding
        wxwidgets fltk motif tk sddm gdm lightdm screencast emerald compiz"""),
    ("Graphics", """opengl vulkan egl gles1 gles2 glamor libglvnd vaapi vdpau nvenc cuda
        opencl xa libkms libdrm drm d3d9 osmesa llvm gallium wayland-egl gbm"""),
    ("Audio", """alsa pulseaudio pipewire jack oss sound openal portaudio sndio
        libsamplerate soxr sndfile audiofile fluidsynth timidity midi mikmod bs2b ladspa
        lv2 speex echo-cancel"""),
    ("Video and codecs", """ffmpeg system-ffmpeg postproc libav x264 x265 xvid theora vpx aom
        dav1d mp3 mp4 mpeg ogg vorbis flac wavpack aac a52 dts lame opus matroska wma wmv
        dvd css vcd mad mpg123 musepack webm mkv"""),
    ("Images", """jpeg jpeg2000 png gif svg tiff webp imlib lcms exif raw heif avif jpegxl
        openexr wmf gimp imagemagick graphicsmagick"""),
    ("Networking", """ipv6 ssl tls curl http http2 wifi iwd networkmanager bluetooth bluez
        avahi zeroconf mdnsresponder-compat upnp ldap kerberos samba ads nfs ftp ssh
        sftp vnc rdp dbus-network libproxy proxy socks5 tor rss xmpp irc mail imap
        pop3 smtp sasl"""),
    ("Hardware and devices", """usb udisks udev lm-sensors hddtemp apcupsd acpi apm upower
        wacom tablet tslib joystick bluetooth scanner sane gphoto2 v4l v4l2 webcam
        smartcard pcsc-lite libinput evdev synaptics battery"""),
    ("System and services", """systemd elogind consolekit openrc dbus policykit pam
        selinux apparmor caps seccomp xattr acl cgroups logrotate cron syslog
        sysvinit split-usr multilib static static-pie"""),
    ("Security and crypto", """crypt openssl gnutls nss libressl gcrypt nettle sodium gpg
        gpgme keyring tpm hardened pie ssp"""),
    ("Languages and scripting", """python perl lua ruby tcl java mono guile php javascript
        nodejs rust go vala introspection script gcj"""),
    ("Compression", """zlib bzip2 lzma xz zstd lz4 lzo brotli gzip zip unrar 7zip"""),
    ("Data and formats", """sqlite mysql mariadb postgres berkdb gdbm odbc xml json yaml
        icu pdf postscript ncurses readline slang unicode"""),
    ("Printing and discs", """cups print printer cdr dvdr cdda cddb cdio libcdio blu-ray
        bluray"""),
    ("Language and translation", """nls l10n i18n spell hunspell aspell enchant"""),
    ("Development and docs", """doc test debug examples static-libs man info vim-syntax
        nano-syntax emacs bash-completion zsh-completion fish-completion valgrind
        profile lto pgo"""),
)
_FLAG_CATEGORY = {flag: title for title, flags in CATEGORIES for flag in flags.split()}
_WORDS = (
    ("Audio", ("audio", "sound", "alsa", "pulseaudio")),
    ("Video and codecs", ("video", "codec", "decoder", "encoder", "mpeg", "multimedia")),
    ("Images", ("image", "picture", "photo")),
    ("Graphics", ("opengl", "graphics", "gpu", "rendering", "3d")),
    ("Networking", ("network", "protocol", "server", "client", "remote", "internet")),
    ("Hardware and devices", ("hardware", "device", "driver", "sensor", "usb")),
    ("Security and crypto", ("encrypt", "crypt", "security", "certificate")),
    ("Languages and scripting", ("bindings", "scripting", "interpreter", "language support")),
    ("Compression", ("compress",)),
    ("Desktop and toolkits", ("desktop", "toolkit", "x11", "gui", "widget")),
    ("Development and docs", ("documentation", "debug", "test suite", "developer")),
)
SPECIFIC = "For specific packages"
OTHER = "Other"


def categorise(flag, descriptions, local):
    """The category of a flag (Porthole's own grouping)"""
    if flag in _FLAG_CATEGORY:
        return _FLAG_CATEGORY[flag]
    text = (descriptions.get(flag) or "").lower()
    for title, words in _WORDS:
        if any(w in text for w in words):
            return title
    if flag in local:
        return SPECIFIC
    return OTHER


def category_order():
    return [title for title, _flags in CATEGORIES] + [SPECIFIC, OTHER]


# -- repositories ------------------------------------------------------------------

REPO_LIST_URL = "https://api.gentoo.org/overlays/repositories.xml"
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SYNC_TYPES = ("git", "rsync", "webrsync", "mercurial", "svn", "laymansync", "zipfile")


def repos_conf_sections(portage_dir="/etc/portage"):
    """{repo name: file it is defined in} from repos.conf (file or directory)"""
    import configparser
    path = os.path.join(portage_dir, "repos.conf")
    files = ([os.path.join(path, f) for f in sorted(os.listdir(path))
              if f.endswith(".conf") or "." not in f] if os.path.isdir(path)
             else [path] if os.path.exists(path) else [])
    found = {}
    for name in files:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(name)
        except configparser.Error:
            continue
        for section in parser.sections():
            if section != "DEFAULT":
                found[section] = name
    return found


def installed_from(repo, root="/"):
    """Installed packages (cpv) that came from a repository"""
    import glob
    found = []
    for path in glob.glob(os.path.join(root, "var", "db", "pkg", "*", "*", "repository")):
        try:
            with open(path) as f:
                if f.read().strip() == repo:
                    cat, pf = path.split(os.sep)[-3:-1]
                    found.append("%s/%s" % (cat, pf))
        except OSError:
            continue
    return sorted(found)


def repositories():
    """The repositories portage uses now: [{name, location, sync_type,
    sync_uri, priority, auto_sync, file, main, installed}]"""
    try:
        import portage
        settings = portage.settings
        repos = list(settings.repositories)
        main = settings.repositories.mainRepo()
        main_name = main.name if main else "gentoo"
    except Exception:
        return []
    files = repos_conf_sections()
    counts = {}
    import glob
    for path in glob.glob("/var/db/pkg/*/*/repository"):
        try:
            with open(path) as f:
                name = f.read().strip()
        except OSError:
            continue
        counts[name] = counts.get(name, 0) + 1
    return [{"name": r.name, "location": r.location, "sync_type": r.sync_type or "",
             "sync_uri": r.sync_uri or "", "priority": r.priority,
             "auto_sync": str(getattr(r, "auto_sync", "") or ""),
             "file": files.get(r.name), "main": r.name == main_name,
             "installed": counts.get(r.name, 0)}
            for r in sorted(repos, key=lambda r: (r.name != main_name, r.name))]


def official_repos(cache_dir=None, max_age=24 * 3600, fetch=True):
    """Gentoo's list of repositories (what eselect repository offers):
    [{name, description, homepage, status, quality, sources: [(type, url)]}].
    Fetched as the user, kept a day."""
    import time
    import urllib.request
    import xml.etree.ElementTree as ET
    cache_dir = cache_dir or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "porthole")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "repositories.xml")
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age
    if fetch and not fresh:
        with urllib.request.urlopen(REPO_LIST_URL, timeout=60) as response:
            data = response.read()
        with open(path + ".new", "wb") as f:
            f.write(data)
        os.replace(path + ".new", path)
    repos = []
    for repo in ET.parse(path).getroot().findall("repo"):
        sources = [(s.get("type"), (s.text or "").strip()) for s in repo.findall("source")]
        repos.append({"name": (repo.findtext("name") or "").strip(),
                      "description": " ".join((repo.findtext("description") or "").split()),
                      "homepage": (repo.findtext("homepage") or "").strip(),
                      "status": repo.get("status", ""), "quality": repo.get("quality", ""),
                      "sources": sources})
    return repos


def check_repo_uri(uri):
    """An address a repository may sync from: one word, a known scheme"""
    if not uri or any(c.isspace() for c in uri) or len(uri) > 400:
        raise ValueError("Not a repository address: %r" % uri)
    if not re.match(r"^(https?|git|rsync|ssh|svn|svn\+ssh)://", uri) and \
            not re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$", uri):
        raise ValueError("Not an address Porthole knows how to sync from: %s" % uri)
    return uri
