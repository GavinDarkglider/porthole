"""
    Porthole crossdev plugin: enough setup that a system boots, lets you
    log in and gets online

    Not a whole-system configurator: Porthole running on the system takes
    over from there. Settings no package installs (hostname, timezone,
    language, keyboard, passwords, network at boot) are kept in
    /var/lib/porthole/firstboot/<name>.json (root's only: it holds password
    hashes) and written into the system at the end of its build, after
    the packages, so they don't collide with the files packages install.

    For each item the window shows where it comes from: a package of the
    system (a board's repository often ships its own, like the Switch's),
    this setup, or nothing yet.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import glob
import json
import os
import re
import subprocess
import time

STATE_DIR = "/var/lib/porthole/firstboot"
ZONEINFO = "/usr/share/zoneinfo"
SUPPORTED = "/usr/share/i18n/SUPPORTED"
KEYMAPS = ("us", "uk", "de", "de-latin1", "fr", "fr-latin1", "es", "it", "pt-latin1",
           "br-abnt2", "se-latin1", "no-latin1", "dk-latin1", "fi", "nl", "pl", "cz",
           "hu", "ru", "jp106", "dvorak", "colemak")
NETWORK = {"networkmanager": ("net-misc/networkmanager", "NetworkManager",
                              "NetworkManager (Wi-Fi and wired; nmtui to connect)"),
           "dhcpcd": ("net-misc/dhcpcd", "dhcpcd", "dhcpcd (wired, Wi-Fi with wpa_supplicant)"),
           "none": (None, None, "Nothing at boot")}
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
_HASH = re.compile(r"^\$(6|y|5)\$[./A-Za-z0-9$]{8,200}$")
_LOCALE = re.compile(r"^[A-Za-z0-9_.@-]{2,40}$")
_ZONE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")

# where each item ends up, for "who sets it" (OpenRC, systemd)
FILES = {"hostname": ("etc/conf.d/hostname", "etc/hostname"),
         "timezone": ("etc/localtime", "etc/localtime"),
         "locale": ("etc/locale.gen", "etc/locale.gen"),
         "keymap": ("etc/conf.d/keymaps", "etc/vconsole.conf"),
         "fstab": ("etc/fstab", "etc/fstab")}
TITLES = {"hostname": "Computer name", "timezone": "Time zone", "locale": "Language",
          "keymap": "Keyboard", "root": "root password", "user": "Your account",
          "network": "Network at boot", "sshd": "Log in over the network (sshd)",
          "fstab": "Disks to mount (fstab)"}


class FirstBootError(Exception):
    pass


def hash_password(password):
    """A SHA-512 crypt hash (the password goes to openssl on stdin)"""
    if not password:
        raise FirstBootError("No password given")
    result = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n",
                            capture_output=True, text=True)
    if result.returncode != 0 or not _HASH.match(result.stdout.strip()):
        raise FirstBootError("openssl couldn't hash the password: %s" % result.stderr.strip())
    return result.stdout.strip()


def zones(zoneinfo=None):
    zoneinfo = zoneinfo or ZONEINFO
    found = []
    for path in glob.glob(os.path.join(zoneinfo, "*", "*")) + glob.glob(
            os.path.join(zoneinfo, "*", "*", "*")):
        name = os.path.relpath(path, zoneinfo)
        if os.path.isfile(path) and name.split("/")[0] not in ("posix", "right", "Etc") \
                and _ZONE.match(name):
            found.append(name)
    return sorted(found) + ["UTC"]


def locales(supported=None):
    try:
        with open(supported or SUPPORTED) as f:
            return [l.split()[0] for l in f if l.strip() and not l.startswith("#")
                    and l.split()[0].endswith(("UTF-8", ".utf8"))]
    except OSError:
        return ["en_US.UTF-8", "C.UTF-8"]


def init_system(target):
    """"systemd" or "openrc", from the system's profile"""
    link = os.path.realpath(os.path.join(target.portage_dir, "make.profile"))
    return "systemd" if "systemd" in link.split("/") else "openrc"


def check(config):
    """The config, checked (ValueError-free: FirstBootError)"""
    if not isinstance(config, dict):
        raise FirstBootError("expected an object")
    out = {}
    host = config.get("hostname")
    if host:
        if not _HOST.match(host):
            raise FirstBootError("A computer name is lower case letters, digits and -")
        out["hostname"] = host
    zone = config.get("timezone")
    if zone:
        if not _ZONE.match(zone) or ".." in zone:
            raise FirstBootError("Not a time zone: %s" % zone)
        out["timezone"] = zone
    locale = config.get("locale")
    if locale:
        if not _LOCALE.match(locale):
            raise FirstBootError("Not a language: %s" % locale)
        out["locale"] = locale
    keymap = config.get("keymap")
    if keymap:
        if not re.match(r"^[A-Za-z0-9_-]{1,30}$", keymap):
            raise FirstBootError("Not a keyboard layout: %s" % keymap)
        out["keymap"] = keymap
    if config.get("root_hash"):
        if not _HASH.match(config["root_hash"]):
            raise FirstBootError("The root password isn't a password hash")
        out["root_hash"] = config["root_hash"]
    user = config.get("user")
    if user:
        if not _USER.match(user.get("name", "")) or user["name"] == "root":
            raise FirstBootError("A user name is lower case letters, digits, _ and -")
        if user.get("hash") and not _HASH.match(user["hash"]):
            raise FirstBootError("The user's password isn't a password hash")
        out["user"] = {"name": user["name"], "hash": user.get("hash") or "",
                       "admin": bool(user.get("admin"))}
    network = config.get("network", "networkmanager")
    if network not in NETWORK:
        raise FirstBootError("Not a network choice: %s" % network)
    out["network"] = network
    out["sshd"] = bool(config.get("sshd"))
    return out


def state_file(name, state_dir=None):
    return os.path.join(state_dir or STATE_DIR, name + ".json")


def read(name, state_dir=None):
    try:
        with open(state_file(name, state_dir)) as f:
            return check(json.load(f))
    except (OSError, ValueError, FirstBootError):
        return {}


def public(config):
    """What the front end may see: no hashes, only whether they're set"""
    shown = {k: v for k, v in config.items() if k not in ("root_hash", "user")}
    shown["root_set"] = bool(config.get("root_hash"))
    if config.get("user"):
        shown["user"] = {"name": config["user"]["name"], "admin": config["user"]["admin"],
                         "password_set": bool(config["user"]["hash"])}
    return shown


def save(name, config, state_dir=None, keep_hashes=True):
    """Store a system's setup (root only, 0600). Hashes not given are kept
    from before (the front end never sees them)."""
    config = check(config)
    if keep_hashes:
        old = read(name, state_dir)
        if "root_hash" not in config and old.get("root_hash"):
            config["root_hash"] = old["root_hash"]
        if config.get("user") and not config["user"]["hash"] and old.get("user") \
                and old["user"]["name"] == config["user"]["name"]:
            config["user"]["hash"] = old["user"]["hash"]
    path = state_file(name, state_dir)
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=1)
    os.replace(path + ".new", path)
    # what the window shows, readable without a password: no hashes
    public_path = state_file(name + ".public", state_dir)
    with open(public_path + ".new", "w") as f:
        json.dump(public(config), f, indent=1)
    os.chmod(public_path + ".new", 0o644)
    os.replace(public_path + ".new", public_path)
    return config


def read_public(name, state_dir=None):
    try:
        with open(state_file(name + ".public", state_dir)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remove(name, state_dir=None):
    for path in (state_file(name, state_dir), state_file(name + ".public", state_dir)):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def packages(config):
    """Packages the setup needs in the system (added to its package list)"""
    wanted = []
    package = NETWORK[config.get("network", "networkmanager")][0]
    if package:
        wanted.append(package)
    if config.get("user", {}).get("admin"):
        wanted.append("app-admin/sudo")
    return wanted


# -- who sets what -----------------------------------------------------------------

def owners(root, paths):
    """{path: cat/pkg-ver} of the given files (relative to root) that an
    installed package of the system owns"""
    wanted = {"/" + p.lstrip("/"): p for p in paths}
    found = {}
    for contents in glob.glob(os.path.join(root, "var/db/pkg/*/*/CONTENTS")):
        pkg = os.path.relpath(os.path.dirname(contents), os.path.join(root, "var/db/pkg"))
        try:
            with open(contents, errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] in ("obj", "sym") and parts[1] in wanted:
                        found[wanted[parts[1]]] = pkg
        except OSError:
            continue
    return found


def status(target, config):
    """[(item, title, where it comes from)] for the window"""
    init = init_system(target)
    index = 0 if init == "openrc" else 1
    paths = {item: files[index] for item, files in FILES.items()}
    owned = owners(target.root, paths.values())
    built = os.path.isdir(os.path.join(target.root, "var/db/pkg"))
    rows = []
    for item in ("hostname", "timezone", "locale", "keymap", "root", "user", "network",
                 "sshd", "fstab"):
        set_here = {"root": bool(config.get("root_hash") or config.get("root_set")),
                    "user": bool(config.get("user")),
                    "network": config.get("network") not in (None, "none"),
                    "sshd": bool(config.get("sshd"))}.get(item, bool(config.get(item)))
        path = paths.get(item)
        if set_here:
            source = "Set here"
            if path in owned:
                source += " (over %s's default)" % owned[path]
        elif path in owned:
            source = "From %s" % owned[path]
        elif item == "fstab":
            source = "When the system is written to a disk (the installer)"
        elif item == "sshd":
            source = "Off"
        elif not built:
            source = "Not set (nothing built yet: a package may still set it)"
        else:
            source = "Not set"
        rows.append((item, TITLES[item], source))
    return rows


# -- writing it into the system (end of its build) ---------------------------------

def _write(root, relative, text, mode=0o644):
    path = os.path.join(root, relative)
    if os.path.islink(path) and relative != "etc/localtime":
        os.unlink(path)
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path + ".new", "w") as f:
        f.write(text)
    os.chmod(path + ".new", mode)
    os.replace(path + ".new", path)


def _set_line(root, relative, key, value, quote=True):
    """Set KEY=value in a shell-style config file, keeping its other lines"""
    path = os.path.join(root, relative)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    line = '%s="%s"' % (key, value) if quote else "%s=%s" % (key, value)
    lines = [l for l in lines if not re.match(r"^\s*%s=" % re.escape(key), l)] + [line]
    _write(root, relative, "\n".join(lines) + "\n")


def _passwd_lines(root, name):
    path = os.path.join(root, name)
    try:
        with open(path) as f:
            return f.read().splitlines()
    except OSError:
        return []


def _set_hash(root, user, hashed):
    lines = _passwd_lines(root, "etc/shadow")
    changed = False
    for i, line in enumerate(lines):
        fields = line.split(":")
        if fields[0] == user:
            fields[1] = hashed
            lines[i] = ":".join(fields)
            changed = True
    if not changed:
        # last changed today (0 would make them change it at first login)
        lines.append("%s:%s:%d:0:99999:7:::" % (user, hashed, int(time.time() // 86400)))
    _write(root, "etc/shadow", "\n".join(lines) + "\n", 0o640)


def _add_user(root, user, log):
    passwd = _passwd_lines(root, "etc/passwd")
    if not passwd:
        log("!! No /etc/passwd in the system yet (sys-apps/baselayout): %s not added"
            % user["name"])
        return
    if any(l.split(":")[0] == user["name"] for l in passwd):
        log("%s is there already" % user["name"])
    else:
        used = {int(l.split(":")[2]) for l in passwd if l.count(":") >= 6
                and l.split(":")[2].isdigit()}
        uid = next(u for u in range(1000, 60000) if u not in used)
        group = _passwd_lines(root, "etc/group")
        gids = {int(l.split(":")[2]) for l in group if l.count(":") >= 3
                and l.split(":")[2].isdigit()}
        gid = uid if uid not in gids else next(g for g in range(1000, 60000) if g not in gids)
        passwd.append("%s:x:%d:%d::/home/%s:/bin/bash" % (user["name"], uid, gid, user["name"]))
        group.append("%s:x:%d:" % (user["name"], gid))
        _write(root, "etc/passwd", "\n".join(passwd) + "\n")
        _write(root, "etc/group", "\n".join(group) + "\n")
        home = os.path.join(root, "home", user["name"])
        os.makedirs(home, mode=0o700, exist_ok=True)
        os.chown(home, uid, gid)
        log("Added the account %s (uid %d)" % (user["name"], uid))
    groups = ["users", "audio", "video", "input", "plugdev"] + (
        ["wheel"] if user["admin"] else [])
    group = _passwd_lines(root, "etc/group")
    for i, line in enumerate(group):
        fields = line.split(":")
        if len(fields) >= 4 and fields[0] in groups:
            members = [m for m in fields[3].split(",") if m]
            if user["name"] not in members:
                fields[3] = ",".join(members + [user["name"]])
                group[i] = ":".join(fields)
    _write(root, "etc/group", "\n".join(group) + "\n")
    _set_hash(root, user["name"], user["hash"] or "!")
    if user["admin"]:
        _write(root, "etc/sudoers.d/porthole-wheel",
               "# Made by Porthole: members of wheel may run anything as root\n"
               "%wheel ALL=(ALL:ALL) ALL\n", 0o440)


def _enable(root, init, service, log):
    if init == "openrc":
        if not os.path.exists(os.path.join(root, "etc/init.d", service)):
            log("!! %s isn't installed in the system: not started at boot" % service)
            return
        link = os.path.join(root, "etc/runlevels/default", service)
        os.makedirs(os.path.dirname(link), mode=0o755, exist_ok=True)
        if not os.path.lexists(link):
            os.symlink("/etc/init.d/" + service, link)
    else:
        unit = service + ".service"
        for d in ("usr/lib/systemd/system", "lib/systemd/system", "etc/systemd/system"):
            if os.path.exists(os.path.join(root, d, unit)):
                break
        else:
            log("!! %s isn't installed in the system: not started at boot" % unit)
            return
        link = os.path.join(root, "etc/systemd/system/multi-user.target.wants", unit)
        os.makedirs(os.path.dirname(link), mode=0o755, exist_ok=True)
        if not os.path.lexists(link):
            os.symlink("/usr/lib/systemd/system/" + unit, link)
    log("Starts at boot: %s" % service)


def apply(target, config, log=lambda m: None):
    """Write the setup into the system (after its packages are installed)"""
    root, init = target.root, init_system(target)
    if config.get("hostname"):
        if init == "openrc":
            _set_line(root, "etc/conf.d/hostname", "hostname", config["hostname"])
        _write(root, "etc/hostname", config["hostname"] + "\n")
        log("Computer name: %s" % config["hostname"])
    if config.get("timezone"):
        zone = os.path.join(root, "usr/share/zoneinfo", config["timezone"])
        if not os.path.exists(zone):
            log("!! %s isn't in the system (sys-libs/timezone-data)" % config["timezone"])
        else:
            link = os.path.join(root, "etc/localtime")
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink("../usr/share/zoneinfo/" + config["timezone"], link)
            _write(root, "etc/timezone", config["timezone"] + "\n")
            log("Time zone: %s" % config["timezone"])
    if config.get("locale"):
        locale = config["locale"]
        charset = "UTF-8" if locale.endswith(("UTF-8", "utf8")) else locale.split(".")[-1]
        path = os.path.join(root, "etc/locale.gen")
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        entry = "%s %s" % (locale, charset)
        if entry not in lines:
            lines.append(entry)
        if "C.UTF-8 UTF-8" not in lines:
            lines.append("C.UTF-8 UTF-8")
        _write(root, "etc/locale.gen", "\n".join(lines) + "\n")
        _write(root, "etc/env.d/02locale", 'LANG="%s"\nLC_COLLATE="C.UTF-8"\n' % locale)
        log("Language: %s (locale-gen makes it on the system's first build step "
            "that runs there, or run it once on the system)" % locale)
    if config.get("keymap"):
        if init == "openrc":
            _set_line(root, "etc/conf.d/keymaps", "keymap", config["keymap"])
        else:
            _set_line(root, "etc/vconsole.conf", "KEYMAP", config["keymap"], quote=False)
        log("Keyboard: %s" % config["keymap"])
    if config.get("root_hash"):
        if _passwd_lines(root, "etc/shadow"):
            _set_hash(root, "root", config["root_hash"])
            log("root password set")
        else:
            log("!! No /etc/shadow in the system yet (sys-apps/baselayout): root password "
                "not set")
    if config.get("user"):
        _add_user(root, config["user"], log)
    service = NETWORK[config.get("network", "networkmanager")][1]
    if service:
        _enable(root, init, service, log)
    if config.get("sshd"):
        _enable(root, init, "sshd", log)
    return True
