"""
    Porthole crossdev plugin: the cross toolchain a system needs

    Worked out from the system's own profile, not from a guide: the
    versions of the kernel headers, C library, gcc and binutils its
    profile and repositories allow (a board's repository may carry its own
    patched ones, like the Switch's glibc and 4.9 headers), and where each
    comes from. crossdev is then told exactly those versions, and the
    repository of each that isn't gentoo's (crossdev otherwise takes the
    first repository that has the package).

    The cross gcc is then the same version as the system's own, so the
    system's builds can hand compiling to this computer with distcc.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import glob
import os

# part, package, crossdev's version option, crossdev's overlay option
PARTS = (("kernel", "sys-kernel/linux-headers", "--k", "--ov-kernel"),
         ("libc", None, "--l", "--ov-libc"),
         ("gcc", "sys-devel/gcc", "--g", "--ov-gcc"),
         ("binutils", "sys-devel/binutils", "--b", "--ov-binutils"))
LIBCS = {"glibc": "sys-libs/glibc", "musl": "sys-libs/musl"}
TITLES = {"kernel": "Kernel headers", "libc": "C library", "gcc": "gcc",
          "binutils": "binutils"}


class ToolchainError(Exception):
    pass


def host_chost():
    try:
        import portage
        return portage.settings["CHOST"]
    except Exception:
        return None


def needs_cross(resolved, host=None):
    """False when the system is for this computer's own CHOST: its own
    compiler builds for it (crossdev refuses to make one for the same
    name). Another C library on the same CPU (musl) is another CHOST."""
    return resolved["tuple"] != (host or host_chost())


def resolve(target):
    """{"tuple": CHOST, "parts": [{"part", "cp", "cpv", "version", "repo",
    "location"}]} for a target (a Target), from its own config"""
    import portage
    from portage.versions import cpv_getversion
    root = target.root
    try:
        settings = portage.config(config_root=target.config_root, target_root=root,
                                  sysroot=target.sysroot, eprefix="")
    except Exception as e:
        raise ToolchainError("Portage can't read %s's settings: %s" % (target.name, e))
    chost = settings.get("CHOST", "")
    if not chost:
        raise ToolchainError("%s's profile sets no CHOST" % target.name)
    elibc = settings.get("ELIBC", "glibc") or "glibc"
    if elibc not in LIBCS:
        raise ToolchainError("Porthole doesn't know the C library %s yet" % elibc)
    portdb = portage.portdbapi(mysettings=settings)
    parts = []
    for part, cp, _flag, _ov in PARTS:
        cp = cp or LIBCS[elibc]
        cpv = portdb.xmatch("bestmatch-visible", cp)
        if not cpv:
            raise ToolchainError("%s's profile allows no version of %s" % (target.name, cp))
        repo = portdb.aux_get(cpv, ["repository"])[0]
        parts.append({"part": part, "cp": cp, "cpv": cpv, "version": cpv_getversion(cpv),
                      "repo": repo, "location": portdb.getRepositoryPath(repo)})
    return {"tuple": chost, "parts": parts}


def command(resolved, output_repo):
    """crossdev's arguments for the resolved toolchain, into output_repo
    (crossdev's repository)"""
    argv = ["crossdev", "--target", resolved["tuple"], "--ov-output", output_repo,
            "--show-fail-log"]
    options = {p[0]: p[2:] for p in PARTS}
    for found in resolved["parts"]:
        flag, ov = options[found["part"]]
        argv += [flag, "=" + found["version"]]
        if found["repo"] != "gentoo":
            argv += [ov, found["location"]]
    return argv


def installed(chost, vdb="/var/db/pkg"):
    """{cp in the cross category: version} of the cross toolchain built here"""
    from portage.versions import catpkgsplit
    found = {}
    for path in glob.glob(os.path.join(vdb, "cross-" + chost, "*")):
        split = catpkgsplit("cross-%s/%s" % (chost, os.path.basename(path)))
        if split:
            cat, pkg, ver, rev = split
            found[pkg] = ver + ("" if rev == "r0" else "-" + rev)
    return found


def compare(resolved, vdb="/var/db/pkg"):
    """[(part title, wanted version, repo, built version or None)]"""
    have = installed(resolved["tuple"], vdb)
    return [(TITLES[p["part"]], p["version"], p["repo"],
             have.get(p["cp"].split("/", 1)[1])) for p in resolved["parts"]]


def unmask_lines(resolved):
    """package.unmask lines for exactly the pinned versions: crossdev masks
    >=9999 of each package as "live" unless the version is exactly 9999,
    which also hides real versions numbered 9999.N (the Switch overlay's
    patched kernel headers are 9999.512)"""
    category = "cross-" + resolved["tuple"]
    return ["=%s/%s-%s" % (category, p["cp"].split("/", 1)[1], p["version"])
            for p in resolved["parts"]]


def write_unmask(resolved, portage_dir="/etc/portage"):
    """Porthole's own package.unmask file for this toolchain (replaced
    each time); returns its path"""
    directory = os.path.join(portage_dir, "package.unmask")
    if os.path.isfile(directory):
        raise ToolchainError("%s is a single file. Porthole keeps its changes in a file of "
                             "its own: move it into a new directory of that name first."
                             % directory)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    path = os.path.join(directory, "porthole-cross-" + resolved["tuple"])
    with open(path + ".new", "w") as f:
        f.write("# Porthole: the exact cross toolchain versions %s's profile allows.\n"
                "# crossdev masks >=9999 as live versions, which also hides real ones\n"
                "# numbered 9999.N; only these exact versions are unmasked.\n"
                % resolved["tuple"])
        for line in unmask_lines(resolved):
            f.write(line + "\n")
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return path


def pretend_command(resolved):
    """emerge --pretend for exactly the pinned cross packages, without their
    dependencies: are they all visible (not masked, keyworded) here?"""
    category = "cross-" + resolved["tuple"]
    return ["emerge", "--pretend", "--oneshot", "--nodeps", "--quiet"] + [
        "=%s/%s-%s" % (category, p["cp"].split("/", 1)[1], p["version"])
        for p in resolved["parts"]]
