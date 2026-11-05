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

    Each cross toolchain is installed here under a name of its own (its
    tuple, e.g. aarch64-gcc16-linux-gnu: the vendor part names it), and
    systems wanting the same compiler share one: the same CPU, OS and C
    library kind, gcc and binutils (the C library and kernel headers inside
    a cross toolchain only build the compiler itself; a system's builds use
    its own, from its root as their sysroot). A system whose settings ask
    for another gcc gets a new toolchain; the others keep theirs, untouched.
    Inside the system its CHOST stays as its profile says
    (aarch64-unknown-linux-gnu): Porthole's compiler wrappers for it run
    its toolchain's programs under the system's names.

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

# the toolchains built here, and which system uses which
REGISTRY = "/var/lib/porthole/toolchains.json"

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


# -- one toolchain per compiler, shared ------------------------------------------------

def split_tuple(chost):
    """(cpu, vendor, the rest) of a CHOST: aarch64-unknown-linux-gnu ->
    ("aarch64", "unknown", "linux-gnu"); a three-part one has no vendor"""
    parts = chost.split("-")
    if len(parts) >= 4:
        return parts[0], parts[1], "-".join(parts[2:])
    return parts[0], "", "-".join(parts[1:])


def _version(resolved, part):
    return next((p["version"] for p in resolved["parts"] if p["part"] == part), None)


def key(resolved):
    """What makes two systems' cross compilers the same one"""
    cpu, _vendor, rest = split_tuple(resolved["tuple"])
    return [cpu, rest, _version(resolved, "gcc"), _version(resolved, "binutils")]


def read_registry(path=None):
    path = path or REGISTRY
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("toolchains", {})
    data.setdefault("systems", {})
    return data


def write_registry(data, path=None):
    path = path or REGISTRY
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".new", "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(path + ".new", path)


def _built(tuple_, want, vdb):
    """True when the toolchain installed as tuple_ has want's gcc and binutils"""
    have = installed(tuple_, vdb)
    return have.get("gcc") == want[2] and have.get("binutils") == want[3]


def find(resolved, vdb="/var/db/pkg", registry=None):
    """The installed toolchain the system can use (its tuple), or None:
    one Porthole built with the same key, or one built before toolchains
    were shared, under the system's own CHOST, with the same gcc and
    binutils"""
    data = registry if registry is not None else read_registry()
    want = key(resolved)
    for tuple_, entry in sorted(data["toolchains"].items()):
        if entry.get("key") == want and _built(tuple_, want, vdb):
            return tuple_
    if _built(resolved["tuple"], want, vdb):
        return resolved["tuple"]
    return None


def new_name(resolved, vdb="/var/db/pkg", registry=None):
    """A tuple for a new toolchain: the vendor part names its gcc
    (aarch64-gcc16-linux-gnu), with a letter for another one with the
    same major version (aarch64-gcc16b-linux-gnu)"""
    data = registry if registry is not None else read_registry()
    cpu, _vendor, rest = split_tuple(resolved["tuple"])
    major = re.match(r"\d+", _version(resolved, "gcc") or "")
    base = "gcc" + (major.group(0) if major else "x")
    for suffix in [""] + [chr(c) for c in range(ord("b"), ord("z") + 1)]:
        tuple_ = "%s-%s%s-%s" % (cpu, base, suffix, rest)
        taken = tuple_ in data["toolchains"] or installed(tuple_, vdb)
        if not taken:
            return tuple_
    raise ToolchainError("No free name for another %s toolchain" % base)


def remember(tuple_, resolved, system, path=None):
    """Note the toolchain (built or adopted) and that system uses it"""
    data = read_registry(path)
    entry = data["toolchains"].setdefault(tuple_, {})
    entry.update(key=key(resolved), versions={p["part"]: p["version"]
                                              for p in resolved["parts"]})
    data["systems"][system] = tuple_
    write_registry(data, path)


def for_system(target, resolved=None, vdb="/var/db/pkg"):
    """(tuple, resolved) of the toolchain the system builds with, or
    (None, resolved) when none is built for what its settings want"""
    resolved = resolved or resolve(target)
    return find(resolved, vdb), resolved


def users(tuple_, registry=None):
    data = registry if registry is not None else read_registry()
    return sorted(name for name, used in data["systems"].items() if used == tuple_)


def unused(registry=None, systems=()):
    """Toolchains no system (of systems: their names) uses any more"""
    data = registry if registry is not None else read_registry()
    used = {data["systems"].get(name) for name in systems}
    return sorted(t for t in data["toolchains"] if t not in used)


def command(resolved, output_repo, tuple_=None):
    """crossdev's arguments for the resolved toolchain, into output_repo
    (crossdev's repository), installed as tuple_ (default: the system's
    CHOST)"""
    argv = ["crossdev", "--target", tuple_ or resolved["tuple"], "--ov-output", output_repo,
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


def compare(resolved, vdb="/var/db/pkg", tuple_=None):
    """[(part title, wanted version, repo, built version or None)]"""
    have = installed(tuple_ or find(resolved, vdb) or resolved["tuple"], vdb)
    return [(TITLES[p["part"]], p["version"], p["repo"],
             have.get(p["cp"].split("/", 1)[1])) for p in resolved["parts"]]


def unmask_lines(resolved, tuple_=None):
    """package.unmask lines for exactly the pinned versions: crossdev masks
    >=9999 of each package as "live" unless the version is exactly 9999,
    which also hides real versions numbered 9999.N (the Switch overlay's
    patched kernel headers are 9999.512)"""
    category = "cross-" + (tuple_ or resolved["tuple"])
    return ["=%s/%s-%s" % (category, p["cp"].split("/", 1)[1], p["version"])
            for p in resolved["parts"]]


def write_unmask(resolved, portage_dir="/etc/portage", tuple_=None):
    """Porthole's own package.unmask file for this toolchain (replaced
    each time); returns its path"""
    directory = os.path.join(portage_dir, "package.unmask")
    if os.path.isfile(directory):
        raise ToolchainError("%s is a single file. Porthole keeps its changes in a file of "
                             "its own: move it into a new directory of that name first."
                             % directory)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    tuple_ = tuple_ or resolved["tuple"]
    path = os.path.join(directory, "porthole-cross-" + tuple_)
    with open(path + ".new", "w") as f:
        f.write("# Porthole: the exact cross toolchain versions for %s (%s).\n"
                "# crossdev masks >=9999 as live versions, which also hides real ones\n"
                "# numbered 9999.N; only these exact versions are unmasked.\n"
                % (tuple_, resolved["tuple"]))
        for line in unmask_lines(resolved, tuple_):
            f.write(line + "\n")
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return path


def pretend_command(resolved, tuple_=None):
    """emerge --pretend for exactly the pinned cross packages, without their
    dependencies: are they all visible (not masked, keyworded) here?"""
    category = "cross-" + (tuple_ or resolved["tuple"])
    return ["emerge", "--pretend", "--oneshot", "--nodeps", "--quiet"] + [
        "=%s/%s-%s" % (category, p["cp"].split("/", 1)[1], p["version"])
        for p in resolved["parts"]]
