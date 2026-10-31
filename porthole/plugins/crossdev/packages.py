"""
    Porthole crossdev plugin: what a system installs

    The list is the system's own world file (var/lib/portage/world), the
    way Gentoo keeps it anyway: building the system installs @world, and
    Porthole on the system finds the same list there. USE flags for single
    packages go in Porthole's file etc/portage/package.use/00-porthole-packages.

    Starter lists are generic (no board presets); a board's repository
    that the system's profile comes from may have a meta package (like the
    Switch overlay's sys-apps/nintendo-switch-meta): it is offered as the
    board's base.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import re

WORLD = "var/lib/portage/world"
USE_FILE = "00-porthole-packages"
_ATOM = re.compile(r"^[A-Za-z0-9+_][A-Za-z0-9+_.-]*/[A-Za-z0-9+_][A-Za-z0-9+_-]*"
                   r"(:[A-Za-z0-9+_.-]+)?(::[A-Za-z0-9_-]+)?$")
_FLAG = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9+_@.-]*$")

STARTERS = (
    ("Small X11 desktop: Xfce's window manager and panel, Firefox, Porthole",
     "A light desktop (only Xfce's window manager, panel, session and terminal, not all "
     "of Xfce) to look things up, read Porthole's help and keep building from. Firefox "
     "comes ready-made from Mozilla (www-client/firefox-bin), so it isn't compiled. "
     "lightdm starts the desktop at boot.",
     ("x11-base/xorg-server", "x11-apps/xinit", "x11-apps/xrandr", "xfce-base/xfwm4",
      "xfce-base/xfce4-panel", "xfce-base/xfce4-session", "xfce-base/xfce4-settings",
      "x11-terms/xfce4-terminal", "x11-misc/lightdm", "x11-misc/lightdm-gtk-greeter",
      "media-fonts/dejavu", "www-client/firefox-bin", "net-misc/networkmanager",
      "gnome-extra/nm-applet", "app-portage/porthole", "app-editors/nano")),
    ("Text only: tools to finish by hand",
     "No desktop: an editor, network tools and Porthole's background parts.",
     ("app-editors/nano", "net-misc/networkmanager", "net-misc/openssh",
      "app-portage/gentoolkit", "app-portage/porthole")),
)


class PackagesError(Exception):
    pass


def check_atom(atom):
    if not _ATOM.match(atom or ""):
        raise PackagesError("Not a package name: %r" % atom)
    return atom


def world_path(target):
    return os.path.join(target.root, WORLD)


def use_path(target):
    return os.path.join(target.portage_dir, "package.use", USE_FILE)


def read_world(target):
    try:
        with open(world_path(target)) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def read_use(target):
    """{atom: [flags]} Porthole set for single packages"""
    found = {}
    try:
        with open(use_path(target)) as f:
            for line in f:
                words = line.split("#", 1)[0].split()
                if len(words) >= 2:
                    found[words[0]] = words[1:]
    except OSError:
        pass
    return found


def check(request):
    """{"world": [atoms], "use": {atom: [flags]}}, checked"""
    if not isinstance(request, dict):
        raise PackagesError("expected an object")
    world = [check_atom(a) for a in request.get("world") or []]
    use = {}
    for atom, flags in (request.get("use") or {}).items():
        check_atom(atom)
        if not isinstance(flags, list) or not all(isinstance(f, str) and _FLAG.match(f)
                                                  for f in flags):
            raise PackagesError("Not USE flags for %s: %r" % (atom, flags))
        if flags:
            use[atom] = flags
    return {"world": list(dict.fromkeys(world)), "use": use}


def _write(path, text):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path + ".new", "w") as f:
        f.write(text)
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)


def save(target, request):
    request = check(request)
    _write(world_path(target), "".join(a + "\n" for a in request["world"]))
    directory = os.path.dirname(use_path(target))
    if os.path.isfile(directory):
        raise PackagesError("%s is a single file; Porthole needs it to be a folder"
                            % directory)
    if request["use"]:
        _write(use_path(target),
               "# USE flags for single packages, set in Porthole (the system's Packages tab).\n"
               "# Porthole replaces this file; other files here are yours.\n"
               + "".join("%s %s\n" % (atom, " ".join(flags))
                         for atom, flags in sorted(request["use"].items())))
    elif os.path.exists(use_path(target)):
        os.unlink(use_path(target))
    return request


# -- looking packages up in the system's repositories -------------------------------

class Lookup:
    """The system's view of its repositories (its own portage config)"""

    def __init__(self, target):
        import portage
        self.settings = portage.config(config_root=target.config_root,
                                       target_root=target.root, sysroot=target.sysroot,
                                       eprefix="")
        self.portdb = portage.portdbapi(mysettings=self.settings)
        self._all = None

    def all(self):
        if self._all is None:
            self._all = self.portdb.cp_all()
        return self._all

    def find(self, text, limit=60):
        """[cat/pkg] whose name holds text"""
        text = text.lower().strip()
        if not text:
            return []
        exact = [cp for cp in self.all() if cp.split("/")[1].lower() == text]
        rest = [cp for cp in self.all() if text in cp.lower() and cp not in exact]
        return (exact + sorted(rest, key=len))[:limit]

    def describe(self, cp):
        """{"cp", "cpv" (best visible, or None), "description", "repo",
        "iuse", "available": why not, or None}"""
        cp = cp.split("::")[0]
        cp = re.sub(r":[^/]*$", "", cp)
        info = {"cp": cp, "cpv": None, "description": "", "repo": None, "iuse": [],
                "problem": None}
        best = self.portdb.xmatch("bestmatch-visible", cp)
        if best:
            info["cpv"] = best
            description, repo, iuse = self.portdb.aux_get(best, ["DESCRIPTION", "repository",
                                                                 "IUSE"])
            info.update(description=description, repo=repo, iuse=iuse.split())
            return info
        every = self.portdb.xmatch("match-all", cp)
        if every:
            description, = self.portdb.aux_get(every[-1], ["DESCRIPTION"])
            info["description"] = description
            info["problem"] = "Every version is masked or not keyworded for this system"
        else:
            info["problem"] = "Not in the repositories this system uses"
        return info

    def use_state(self, cpv):
        """{flag: on} of a package as this system would build it"""
        self.settings.setcpv(cpv, mydb=self.portdb)
        on = set(self.settings["PORTAGE_USE"].split())
        iuse = [f.lstrip("+-") for f in self.portdb.aux_get(cpv, ["IUSE"])[0].split()]
        return {f: f in on for f in iuse}


def board_meta(target, repos):
    """[cat/pkg] meta packages of the repository the system's profile comes
    from, if it isn't gentoo's (a board's base)"""
    link = os.path.realpath(os.path.join(target.portage_dir, "make.profile"))
    for name, location in repos.items():
        if name != "gentoo" and link.startswith(location.rstrip("/") + "/profiles/"):
            found = []
            # the base of the system itself: sys-apps (not, say, games-emulation/*-meta)
            path = os.path.join(location, "sys-apps")
            if os.path.isdir(path):
                found = ["sys-apps/%s" % p for p in sorted(os.listdir(path))
                         if p.endswith("-meta") and os.path.isdir(os.path.join(path, p))]
            return found
    return []


def flag_descriptions(location, cp):
    """{flag: text} from a package's metadata.xml"""
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(os.path.join(location, cp, "metadata.xml"))
    except (OSError, ET.ParseError):
        return {}
    return {flag.get("name"): " ".join("".join(flag.itertext()).split())
            for flag in tree.iter("flag") if flag.get("name")}
