"""
    Porthole crossdev plugin: building a system on this computer

    Cross-compiled with the system's toolchain, the way crossdev's
    cross-emerge does, except that the system's root is also its sysroot
    (one tree: what is built for it is linked against what is in it):

        ROOT=SYSROOT=PORTAGE_CONFIGROOT=<root>  CBUILD=<this computer>

    The cross gcc always looks in /usr/<CHOST> (its built-in sysroot, not
    $SYSROOT), so the build runs with small wrappers first in PATH that add
    --sysroot=<root> (/var/lib/porthole/cross/<name>/bin). crossdev's
    pkg-config wrapper already follows ESYSROOT.

    In order: the kernel headers and C library first (everything else
    links against them), then @system, then the system's own list
    (@world) and what its First boot choices need, then the first-boot
    setup. Packages that fail to cross-compile are listed at the end (the
    emulator fallback builds them in a later version).

    Tools a build runs on this computer (BDEPEND) are installed here, the
    way any cross build does; when they need a change on this computer
    (typically one more Python target, when the system's Python is
    another version), it is shown before building and only made when
    the user agrees, in Porthole's own file
    /etc/portage/package.use/porthole-cross-<name>.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import os
import re
import subprocess

WRAPPERS_DIR = "/var/lib/porthole/cross"
TOOLS = ("gcc", "g++", "cc", "c++", "cpp", "gfortran", "ld", "ld.bfd")
BASE = ("sys-kernel/linux-headers", "sys-libs/glibc")
HOST_FILE = "porthole-cross-%s"
ROOT_FILE = "00-porthole-build"
_LINE = re.compile(r"^(>=|<=|=|~|<|>)?[A-Za-z0-9+_][A-Za-z0-9+_.-]*/[A-Za-z0-9+_][A-Za-z0-9+_.:-]*"
                   r"( +-?[A-Za-z0-9][A-Za-z0-9+_@.:-]*)+$")
_KEYWORD_LINE = re.compile(r"^(>=|<=|=|~|<|>)?[A-Za-z0-9+_][A-Za-z0-9+_.-]*/"
                           r"[A-Za-z0-9+_][A-Za-z0-9+_.:-]*( +~?\*?[a-z0-9-]*\*?)?$")


class BuildError(Exception):
    pass


def host_chost():
    from porthole.plugins.crossdev import toolchain
    return toolchain.host_chost()


def chost_of(target):
    from porthole.plugins.crossdev import conf
    return conf.effective(conf.read(target), "CHOST")


def wrappers_dir(target, base=None):
    return os.path.join(base or WRAPPERS_DIR, target.name, "bin")


def write_wrappers(target, chost, base=None, search="/usr/bin"):
    """The compiler wrappers adding --sysroot=<root>; returns their folder"""
    directory = wrappers_dir(target, base)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    root = os.path.realpath(target.root)
    for tool in TOOLS:
        real = os.path.join(search, "%s-%s" % (chost, tool))
        path = os.path.join(directory, "%s-%s" % (chost, tool))
        if not os.path.exists(real):
            if os.path.exists(path):
                os.unlink(path)
            continue
        with open(path + ".new", "w") as f:
            f.write("#!/bin/sh\n# Porthole: %s for the system in %s (its root is its sysroot)\n"
                    'exec %s --sysroot=%s "$@"\n' % (tool, root, real, root))
        os.chmod(path + ".new", 0o755)
        os.replace(path + ".new", path)
    return directory


def qemu_name(chost):
    """qemu's user-mode emulator for a CHOST's CPU"""
    cpu = chost.split("-")[0]
    if cpu.startswith("armeb"):
        return "qemu-armeb"
    if cpu.startswith("arm"):
        return "qemu-arm"
    if re.match(r"i[3-6]86$", cpu):
        return "qemu-i386"
    return "qemu-" + {"powerpc64": "ppc64", "powerpc64le": "ppc64le",
                      "powerpc": "ppc"}.get(cpu, cpu)


# ELF e_machine and class (1 = 32-bit, 2 = 64-bit) -> qemu's user-mode emulator
# (in big endian where the CPU has both, see qemu_for)
QEMU_MACHINES = {(183, 2): "aarch64", (40, 1): "arm", (62, 2): "x86_64", (3, 1): "i386",
                 (243, 2): "riscv64", (243, 1): "riscv32", (21, 2): "ppc64", (20, 1): "ppc",
                 (258, 2): "loongarch64", (22, 2): "s390x", (8, 1): "mips", (8, 2): "mips64",
                 (2, 1): "sparc", (43, 2): "sparc64", (15, 1): "hppa", (4, 1): "m68k",
                 (36901, 2): "alpha"}


# the same CPU number in little endian
QEMU_LITTLE = {"ppc64": "ppc64le", "mips": "mipsel", "mips64": "mips64el"}
# and in big endian
QEMU_BIG = {"aarch64": "aarch64_be", "arm": "armeb"}


def qemu_for(ident):
    """qemu's user-mode emulator name for an elf_id(), or None"""
    machine, klass, little = ident
    name = QEMU_MACHINES.get((machine, klass))
    if name is None:
        return None
    return QEMU_LITTLE.get(name, name) if little else QEMU_BIG.get(name, name)


def elf_id(path):
    """(e_machine, class, little endian) of an ELF file, or None"""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return None
    little = head[5] == 1
    return struct.unpack("<H" if little else ">H", head[18:20])[0], head[4], little


def elf_machine(path):
    found = elf_id(path)
    return found[0] if found else None


def dynamic_linkers(root):
    """{elf_id: linker path} of every dynamic linker in the system (a
    multilib system has one per ABI)"""
    import glob
    found = {}
    for path in sorted(glob.glob(os.path.join(root, "lib*", "ld-linux*.so*")) +
                       glob.glob(os.path.join(root, "lib*", "ld-musl*.so*")) +
                       glob.glob(os.path.join(root, "lib*", "ld.so.1"))):
        real = os.path.realpath(path)
        ident = elf_id(real)
        if ident and ident not in found:
            found[ident] = real
    return found


def dynamic_linker(root, chost=None):
    """The system's main dynamic linker (its CHOST's ABI when known)"""
    linkers = dynamic_linkers(root)
    if chost:
        wanted = qemu_name(chost)[len("qemu-"):]
        for ident, path in linkers.items():
            if qemu_for(ident) == wanted:
                return path
    # otherwise prefer 64-bit
    for key in sorted(linkers, key=lambda k: -k[1]):
        return linkers[key]
    return None


def write_ldd(target, chost, base=None):
    """An ldd first in PATH that also lists the system's programs, each
    through the qemu and dynamic linker (ld.so --list) of its own ABI
    (a multilib system has several); others go to the real ldd.
    g-ir-scanner runs ldd on the program it builds."""
    root = os.path.realpath(target.root)
    path = os.path.join(wrappers_dir(target, base), "ldd")
    cases = []
    for ident, linker in sorted(dynamic_linkers(root).items()):
        qemu = qemu_for(ident)
        if qemu:
            cases.append('\t%d/%d/%d) exec qemu-%s -L %s %s --list "$last" ;;'
                         % (ident[0], ident[1], 1 if ident[2] else 2, qemu, root, linker))
    if not cases:
        if os.path.exists(path):
            os.unlink(path)
        return None
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path + ".new", "w") as f:
        f.write('#!/bin/sh\n'
                '# Porthole: ldd that also lists programs built for %s (%s)\n'
                'for last; do :; done\n'
                'if [ "$(od -An -c -j1 -N3 "$last" 2>/dev/null | tr -d " ")" = "ELF" ]; then\n'
                '\tclass=$(od -An -tu1 -j4 -N1 "$last" | tr -d " ")\n'
                '\tendian=$(od -An -tu1 -j5 -N1 "$last" | tr -d " ")\n'
                '\tif [ "$endian" = 1 ]; then\n'
                '\t\tmachine=$(od -An -tu1 -j18 -N2 "$last" | awk \'{print $1 + 256 * $2}\')\n'
                '\telse\n'
                '\t\tmachine=$(od -An -tu1 -j18 -N2 "$last" | awk \'{print 256 * $1 + $2}\')\n'
                '\tfi\n'
                '\tcase "$machine/$class/$endian" in\n'
                '%s\n'
                '\tesac\n'
                'fi\n'
                'exec /usr/bin/ldd "$@"\n'
                % (target.name, chost, "\n".join(cases)))
    os.chmod(path + ".new", 0o755)
    os.replace(path + ".new", path)
    return path


BASHRC_BEGIN = "# --- Porthole: cross builds (changed by Porthole) ---"
BASHRC_END = "# --- end of Porthole's cross build settings ---"


def bashrc_block(chost, root):
    return "\n".join([
        BASHRC_BEGIN,
        "# Only when this system is cross-built on another computer (CBUILD set to",
        "# that computer); building on the system itself, or in the emulator, has",
        "# nothing to do here.",
        'if [[ -n ${CBUILD} && ${CBUILD} != "${CHOST}" ]]; then',
        "	# gobject-introspection's g-ir-scanner compiles with $CC (else this",
        "	# computer's own compiler) and runs the program it built through",
        "	# GI_CROSS_LAUNCHER",
        '	export CC="${CC:-${CHOST}-gcc}" CXX="${CXX:-${CHOST}-g++}"',
        '	export GI_CROSS_LAUNCHER="%s -L %s"' % (qemu_name(chost), root),
        "fi",
        BASHRC_END])


def write_bashrc(target, chost):
    """Porthole's block in the system's etc/portage/bashrc (portage reads it
    for the system's own packages only, not for tools built here)"""
    path = os.path.join(target.portage_dir, "bashrc")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        text = ""
    block = bashrc_block(chost, os.path.realpath(target.root))
    if BASHRC_BEGIN in text and BASHRC_END in text:
        before, rest = text.split(BASHRC_BEGIN, 1)
        text = before + block + rest.split(BASHRC_END, 1)[1]
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    with open(path + ".new", "w") as f:
        f.write(text)
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return path


def environment(target, chost, base=None):
    """What emerge needs to build into the system"""
    root = os.path.realpath(target.root)
    return {"ROOT": root, "SYSROOT": root, "PORTAGE_CONFIGROOT": os.path.realpath(
                target.config_root), "CBUILD": host_chost() or "", "CHOST": chost,
            "PATH": wrappers_dir(target, base) + ":" + os.environ.get("PATH", "/usr/bin:/bin")}


def world_extra(target):
    """Packages the First boot choices need that the list doesn't have"""
    from porthole.plugins.crossdev import firstboot, packages
    saved = firstboot.read_public(target.name)
    if not saved:
        return []
    have = set(packages.read_world(target))
    return [a for a in firstboot.packages(saved) if a not in have]


STAGES = (("base", "The kernel headers and C library"),
          ("system", "The base system (@system)"),
          ("extra", "What First boot needs, added to the list"),
          ("world", "The system's own packages (@world)"))


def commands(target, stage):
    """[emerge arguments] for one stage of the build ([]: nothing to do)"""
    common = ["emerge", "--verbose", "--color=y"]
    if stage == "base":
        # one at a time, without their dependencies: in portage's order some
        # (timezone-data, libidn2) come before glibc, and they can't be
        # compiled until glibc's headers and libraries are in the root.
        # glibc itself needs only the kernel headers and the cross compiler.
        # --noreplace: a build started again doesn't redo them
        return [common + ["--oneshot", "--nodeps", "--noreplace", atom] for atom in BASE]
    if stage == "system":
        return [common + ["--update", "--deep", "--newuse", "--keep-going", "@system"]]
    if stage == "extra":
        extra = world_extra(target)
        return [common + ["--noreplace", "--keep-going"] + extra] if extra else []
    if stage == "world":
        return [common + ["--update", "--deep", "--newuse", "--keep-going", "@world"]]
    raise BuildError("No such stage: %s" % stage)


def check_argv(target):
    """emerge --pretend as root before building: everything agreed to is
    written, so it must work out now without further changes"""
    return ["emerge", "--pretend", "--quiet", "--color=y", "--update", "--deep", "--newuse",
            "@system", "@world"] + list(BASE) + world_extra(target)


def pretend_argv(target):
    """One emerge --pretend over everything the build installs"""
    return ["emerge", "--pretend", "--verbose", "--color=n", "--update", "--deep", "--newuse",
            "--autounmask=y", "--autounmask-backtrack=y", "--autounmask-write=n",
            "@system", "@world"] + list(BASE) + world_extra(target)


def parse_pretend(text, root):
    """{"root": [cpv], "host": [cpv], "root_use", "host_use", "root_keywords",
    "host_keywords": [line], "other": [text], "ok": bool} from emerge
    --pretend's output (its changes come under "For <config dir>:")"""
    root = os.path.realpath(root).rstrip("/")
    out = {"root": [], "host": [], "root_use": [], "host_use": [], "root_keywords": [],
           "host_keywords": [], "other": [], "ok": True}
    side, section = "root", None
    for raw in text.splitlines():
        line = raw.rstrip()
        match = re.match(r"^\[(ebuild|binary)[^\]]*\]\s+(\S+)", line)
        if match:
            cpv = re.sub(r"::.*$", "", match.group(2))
            (out["root"] if " to %s/" % root in line else out["host"]).append(cpv)
            continue
        match = re.match(r"^For (/\S*):$", line)
        if match:
            side = "root" if os.path.realpath(match.group(1)).startswith(root + "/") else "host"
            continue
        if "USE changes are necessary" in line:
            section = "use"
            continue
        if "keyword changes are necessary" in line:
            section = "keywords"
            continue
        if re.search(r"(mask|license) changes are necessary", line):
            section = None
            out["ok"] = False
            out["other"].append(line.strip())
            continue
        if section and (not line or line.startswith(("#", " "))):
            continue
        if section == "use" and _LINE.match(line.strip()):
            out[side + "_use"].append(line.strip())
            continue
        if section == "keywords" and _KEYWORD_LINE.match(line.strip()):
            out[side + "_keywords"].append(line.strip())
            continue
        section = None
        if line.startswith(("!!!", "emerge: there are no")):
            out["ok"] = False
            out["other"].append(line.strip())
    if out["host_keywords"]:
        out["ok"] = False
        out["other"].append("Packages built on this computer would need testing versions: "
                            + ", ".join(out["host_keywords"]))
    return out


def plan(target):
    """Work out the build as the user (no root needed): parse_pretend's
    result, with "output" (its end) and "chost" """
    chost = chost_of(target)
    env = dict(os.environ, **environment(target, chost))
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    result = subprocess.run(pretend_argv(target), env=env, capture_output=True, text=True,
                            timeout=1800)
    text = result.stdout + "\n" + result.stderr
    parsed = parse_pretend(text, target.root)
    if result.returncode != 0 and not (parsed["root_use"] or parsed["host_use"]
                                       or parsed["root_keywords"]):
        parsed["ok"] = False
    parsed["output"] = text[-6000:]
    parsed["chost"] = chost
    return parsed


def check_lines(lines, pattern):
    lines = [l.strip() for l in lines or [] if l.strip()]
    for line in lines:
        if not pattern.match(line):
            raise BuildError("Not a package setting: %r" % line)
    return lines


def write_changes(target, host_use, root_use, root_keywords, portage_dir="/etc/portage"):
    """The changes the user agreed to: this computer's in Porthole's own
    file for this system (removed with it), the system's in its own"""
    host_use = check_lines(host_use, _LINE)
    root_use = check_lines(root_use, _LINE)
    root_keywords = check_lines(root_keywords, _KEYWORD_LINE)
    written = []
    for path, lines, what in (
            (os.path.join(portage_dir, "package.use", HOST_FILE % target.name), host_use,
             "this computer: tools that build %s" % target.title),
            (os.path.join(target.portage_dir, "package.use", ROOT_FILE), root_use,
             "%s's packages" % target.title),
            (os.path.join(target.portage_dir, "package.accept_keywords", ROOT_FILE),
             root_keywords, "%s's packages" % target.title)):
        if not lines:
            continue
        directory = os.path.dirname(path)
        if os.path.isfile(directory):
            raise BuildError("%s is a single file; Porthole keeps its changes in a file of "
                             "its own: move it into a new directory of that name first"
                             % directory)
        os.makedirs(directory, mode=0o755, exist_ok=True)
        old = []
        if os.path.exists(path):
            with open(path) as f:
                old = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        merged = list(dict.fromkeys(old + lines))
        with open(path + ".new", "w") as f:
            f.write("# Porthole: changes building %s needs (%s),\n"
                    "# agreed to before the build. Removed with the system.\n"
                    % (target.title, what) + "".join(l + "\n" for l in merged))
        os.chmod(path + ".new", 0o644)
        os.replace(path + ".new", path)
        written.append(path)
    return written


def remove_host_changes(target, portage_dir="/etc/portage"):
    path = os.path.join(portage_dir, "package.use", HOST_FILE % target.name)
    if os.path.exists(path):
        os.unlink(path)
        return path
    return None
