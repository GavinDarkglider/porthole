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
import shutil
import subprocess

WRAPPERS_DIR = "/var/lib/porthole/cross"
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


# the system's programs that are given its root as their sysroot (the rest
# of a toolchain, ar or strip, gets no --sysroot)
_SYSROOT_TOOL = re.compile(r"^(gcc|g\+\+|cc|c\+\+|cpp|gfortran|ld(\.bfd|\.gold)?)(-[0-9.]+)?$")


def write_wrappers(target, chost, base=None, search="/usr/bin", toolchain=None):
    """The system's toolchain programs under its own names (<chost>-gcc,
    <chost>-ar, ...), each running the toolchain's (toolchain: its tuple,
    by default the system's CHOST), compilers and linkers with
    --sysroot=<root>. Wrappers of programs the toolchain doesn't have are
    taken away. Returns their folder."""
    directory = wrappers_dir(target, base)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    root = os.path.realpath(target.root)
    toolchain = toolchain or chost
    prefix = toolchain + "-"
    wanted = set()
    for name in sorted(os.listdir(search)) if os.path.isdir(search) else []:
        real = os.path.join(search, name)
        if not name.startswith(prefix) or not os.access(real, os.X_OK) or os.path.isdir(real):
            continue
        if os.path.basename(os.path.realpath(real)).startswith("cross-"):
            continue  # crossdev's helpers (emerge, pkg-config, ...), not the toolchain's
        tool = name[len(prefix):]
        path = os.path.join(directory, "%s-%s" % (chost, tool))
        wanted.add(path)
        sysroot = " --sysroot=%s" % root if _SYSROOT_TOOL.match(tool) else ""
        with open(path + ".new", "w") as f:
            f.write("#!/bin/sh\n# Porthole: %s for the system in %s (toolchain %s)\n"
                    'exec %s%s "$@"\n' % (tool, root, toolchain, real, sysroot))
        os.chmod(path + ".new", 0o755)
        os.replace(path + ".new", path)
    # pkg-config for the system: crossdev's generic one works out the
    # system from CHOST and SYSROOT (portage sets both), whatever toolchain
    cross_pkg_config = os.path.join(search, "cross-pkg-config")
    if os.path.exists(cross_pkg_config):
        path = os.path.join(directory, chost + "-pkg-config")
        if os.path.lexists(path) and os.path.realpath(path) != cross_pkg_config:
            os.unlink(path)
        if not os.path.lexists(path):
            os.symlink(cross_pkg_config, path)
        wanted.add(path)
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if name.startswith(chost + "-") and path not in wanted and not name.endswith(".new"):
            os.unlink(path)
    return directory


def distcc_names(chost, masquerade="/usr/lib/distcc"):
    """distccd only runs compilers named in its masquerade folder: the
    system's compiler names (its builds in the emulator ask for those),
    made there if missing. Returns the names made."""
    made = []
    if not os.path.isdir(masquerade):
        return made
    distcc = shutil.which("distcc") or "/usr/bin/distcc"
    for tool in ("gcc", "g++", "cc", "c++", "cpp"):
        link = os.path.join(masquerade, "%s-%s" % (chost, tool))
        if not os.path.lexists(link):
            os.symlink(distcc, link)
            made.append(os.path.basename(link))
    return made


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


_PYTHON_ASK = "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX') or '')"


def _python_suffix(argv):
    """The ending a Python gives its C modules (.cpython-313-x86_64-linux-gnu.so),
    asked of that Python, or None"""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    try:
        out = subprocess.run(argv + ["-I", "-c", _PYTHON_ASK], env=env,
                             capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if out.endswith(".so") else None


def system_pythons(root):
    """The system's Python programs (python3.13, python3.13t, ...): the real
    programs in its usr/bin, not the scripts or links next to them"""
    import glob
    found = []
    for path in sorted(glob.glob(os.path.join(root, "usr/bin/python3.*"))):
        if not os.path.islink(path) and elf_id(path):
            found.append(os.path.basename(path))
    return found


def python_suffix_moves(root):
    """[(this computer's C module ending, the system's)] for each Python the
    system has and this computer has the same version of, where they differ
    (a cross build names its modules with this computer's ending). Each
    Python is asked: this computer's directly, the system's through qemu."""
    moves = []
    for name in system_pythons(root):
        program = os.path.join(root, "usr/bin", name)
        qemu = qemu_for(elf_id(program))
        here = shutil.which(name)
        if not qemu or not shutil.which("qemu-" + qemu) or not here:
            continue
        theirs = _python_suffix(["qemu-" + qemu, "-L", root, program])
        mine = _python_suffix([here])
        if theirs and mine and theirs != mine and (mine, theirs) not in moves:
            moves.append((mine, theirs))
    return moves


def misnamed_python_modules(root, moves=None):
    """[(path, correct path)] of Python C modules in the system named with
    this computer's ending (a cross build took it from this computer's
    Python): the system's Python doesn't find them"""
    moves = python_suffix_moves(root) if moves is None else moves
    found = []
    if not moves:
        return found
    for top in ("usr/lib", "usr/lib64"):
        for folder, _dirs, files in os.walk(os.path.join(root, top)):
            for name in files:
                for mine, theirs in moves:
                    if name.endswith(mine):
                        path = os.path.join(folder, name)
                        found.append((path, path[:-len(mine)] + theirs))
                        break
    return found


def link_python_modules(target, log=lambda m: None):
    """For modules already installed under the wrong name: a link with the
    right one next to each (the package's file list stays as it is)"""
    made = 0
    for path, correct in misnamed_python_modules(os.path.realpath(target.root)):
        if os.path.lexists(correct):
            continue
        os.symlink(os.path.basename(path), correct)
        made += 1
    if made:
        log("Linked %d Python module%s installed under this computer's platform name to "
            "the system's (a cross build named them wrongly)" % (
                made, "" if made == 1 else "s"))
    return made


# perl's install folders (perl -V:<name>): where modules with C code, plain
# modules, and their .packlist files go
PERL_DIRS = ("installarchlib", "installprivlib", "installvendorarch",
             "installvendorlib", "installsitearch", "installsitelib")
# of those, the ones only perl modules with C code use (named for one CPU)
PERL_ARCH_DIRS = ("installarchlib", "installvendorarch", "installsitearch")

_PERL_ASK = 'use Config; print "$_=$Config{$_}\\n" for @ARGV'


def _perl_config(argv):
    """perl's own answer for PERL_DIRS: {name: folder}, or {}"""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    try:
        out = subprocess.run(argv + ["-e", _PERL_ASK] + list(PERL_DIRS), env=env,
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {k: v.rstrip("/") for k, v in found.items() if k in PERL_DIRS and v.startswith("/")}


def host_perl_dirs():
    """This computer's perl install folders (the ones a cross build uses)"""
    perl = shutil.which("perl")
    return _perl_config([perl]) if perl else {}


def system_perl_dirs(root):
    """The system's perl install folders, asked of its own perl through qemu,
    or {} (no perl in it yet, or no qemu for it)"""
    perl = os.path.join(root, "usr/bin/perl")
    qemu = qemu_for(elf_id(os.path.realpath(perl)) or (0, 0, True))
    if not os.path.isfile(perl) or not qemu or not shutil.which("qemu-" + qemu):
        return {}
    return _perl_config(["qemu-" + qemu, "-L", root, perl])


def perl_dir_moves(host, system, names=PERL_DIRS):
    """[(this computer's folder, the system's)] where they differ, the
    deepest first (an architecture folder can be inside a library one)"""
    moves = [(host[n], system[n]) for n in names
             if host.get(n) and system.get(n) and host[n] != system[n]]
    return sorted(set(moves), key=lambda m: -m[0].count("/"))


def repair_perl_modules(target, log=lambda m: None, host=None, system=None):
    """Perl modules with C code already installed in this computer's perl
    architecture folders (perl -V:installvendorarch here) by cross builds:
    moved into the system's own, with a link left at the old place so the
    packages' file lists stay valid. A link from an older Porthole that
    points somewhere else is moved on the same way. Only architecture
    folders: nothing of the system's own perl is ever in this computer's
    (a plain library folder of another version may be)."""
    root = os.path.realpath(target.root)
    host = host_perl_dirs() if host is None else host
    system = system_perl_dirs(root) if system is None else system
    moved = 0
    for here, theirs in perl_dir_moves(host, system, PERL_ARCH_DIRS):
        wrong, right = root + here, root + theirs
        if not os.path.lexists(wrong):
            continue
        if os.path.islink(wrong):
            there = os.path.realpath(wrong)
            if there != right and there.startswith(root + "/") and os.path.isdir(there):
                moved += _unmove_perl_modules(root, "/" + os.path.relpath(there, root),
                                              theirs, here, log)
            if there != right:
                os.unlink(wrong)
                os.symlink(os.path.relpath(right, os.path.dirname(wrong)), wrong)
            continue
        if not os.path.isdir(wrong):
            continue
        if os.path.islink(right):
            os.unlink(right)
            aside = right + ".1"
            if os.path.isdir(aside) and not os.path.islink(aside):
                os.rename(aside, right)
        os.makedirs(right, mode=0o755, exist_ok=True)
        for folder, dirs, files in os.walk(wrong):
            into = os.path.join(right, os.path.relpath(folder, wrong))
            os.makedirs(into, exist_ok=True)
            for name in files + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
                source, dest = os.path.join(folder, name), os.path.join(into, name)
                if os.path.lexists(dest):
                    continue  # the system's own build of it wins
                shutil.copy2(source, dest, follow_symlinks=False)
                if name == ".packlist":
                    with open(dest) as f:
                        text = f.read()
                    with open(dest, "w") as f:
                        f.write(text.replace(here + "/", theirs + "/"))
                moved += 1
        shutil.rmtree(wrong)
        os.symlink(os.path.relpath(right, os.path.dirname(wrong)), wrong)
    if moved:
        log("Moved %d perl module file%s from this computer's perl folders to the "
            "system's; a cross build put them there" % (moved, "" if moved == 1 else "s"))
    return moved


def _unmove_perl_modules(root, bad_dir, arch_dir, host_dir, log):
    """Modules an older Porthole moved into bad_dir instead of the system's
    arch_dir (it read perl's architecture from libnet's Net/Config.pm, so
    bad_dir was .../vendor_perl/<version>/Net): moved on, with the installed
    packages' file lists (and .packlists) changed to match. A package with
    C code (a .so under bad_dir/auto) had all its files in perl's
    architecture folder; the others only their .packlist (bad_dir/auto/...),
    as bad_dir may be a real module folder too (Net::HTTP's files in Net/).
    Files a package lists under host_dir (through the link) are just moved:
    the link to arch_dir keeps those lists right. Folders are root-relative."""
    import glob
    import hashlib
    bad_dir, arch_dir, host_dir = bad_dir + "/", arch_dir + "/", host_dir + "/"
    moved = 0
    for vdb in glob.glob(os.path.join(root, "var/db/pkg/*/*")):
        contents = os.path.join(vdb, "CONTENTS")
        try:
            with open(contents) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            kind, _, rest = line.partition(" ")
            if kind == "obj" and rest.startswith(host_dir):
                path = rest.rsplit(" ", 2)[0][len(host_dir):]
                source, dest = root + bad_dir + path, root + arch_dir + path
                if os.path.lexists(source) and not os.path.islink(source):
                    os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
                    if os.path.lexists(dest):
                        os.unlink(source)
                    else:
                        os.rename(source, dest)
                    moved += 1
        if not any(" %sauto/" % bad_dir in line for line in lines):
            continue
        has_c = any(line.startswith("obj %sauto/" % bad_dir) and ".so " in line
                    for line in lines)
        which = bad_dir if has_c else bad_dir + "auto/"
        renames = {}
        for line in lines:
            kind, _, rest = line.partition(" ")
            if kind in ("obj", "sym") and rest.startswith(which):
                path = rest.split(" -> ")[0] if kind == "sym" else rest.rsplit(" ", 2)[0]
                renames[path] = arch_dir + path[len(bad_dir):]
        for old, new in sorted(renames.items()):
            source, dest = root + old, root + new
            if not os.path.lexists(source):
                continue
            os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
            if os.path.lexists(dest):
                os.unlink(source)  # the system's own build of it wins
            else:
                os.rename(source, dest)
            moved += 1
        fixed = []
        for line in lines:
            kind, _, rest = line.partition(" ")
            if kind == "dir" and rest.startswith(which):
                line = "dir " + arch_dir + rest[len(bad_dir):]
            elif kind == "sym" and rest.split(" -> ")[0] in renames:
                path = rest.split(" -> ")[0]
                line = "sym " + renames[path] + rest[len(path):]
            elif kind == "obj" and rest.rsplit(" ", 2)[0] in renames:
                path = renames[rest.rsplit(" ", 2)[0]]
                if path.endswith("/.packlist") and os.path.isfile(root + path):
                    with open(root + path) as f:
                        text = f.read()
                    with open(root + path, "w") as f:
                        for entry in text.splitlines():
                            name = entry.split(" ")[0]
                            f.write(renames.get(name, name) + entry[len(name):] + "\n")
                    with open(root + path, "rb") as f:
                        digest = hashlib.md5(f.read()).hexdigest()
                    line = "obj %s %s %d" % (path, digest, int(os.stat(root + path).st_mtime))
                else:
                    line = "obj " + path + rest[len(rest.rsplit(" ", 2)[0]):]
            fixed.append(line)
        if renames:
            with open(contents, "w") as f:
                f.write("\n".join(fixed) + "\n")
            for name in ("NEEDED", "NEEDED.ELF.2"):
                path = os.path.join(vdb, name)
                if os.path.isfile(path):
                    with open(path) as f:
                        text = f.read()
                    for old, new in renames.items():
                        text = text.replace(old, new)
                    with open(path, "w") as f:
                        f.write(text)
    # folders left empty in bad_dir (bottom up; the real modules there stay)
    top = root + bad_dir.rstrip("/")
    for folder, dirs, files in os.walk(top, topdown=False):
        if folder != top and not os.listdir(folder):
            os.rmdir(folder)
    if moved:
        log("Moved %d perl module file%s from %s to %s (an older Porthole put them "
            "in the wrong folder)" % (moved, "" if moved == 1 else "s",
                                      bad_dir.rstrip("/"), arch_dir.rstrip("/")))
    return moved


BASHRC_BEGIN = "# --- Porthole: cross builds (changed by Porthole) ---"
BASHRC_END = "# --- end of Porthole's cross build settings ---"


def bashrc_block(chost, root, tools=None, python_moves=(), perl_moves=()):
    """Porthole's block in the system's bashrc: only when it is cross-built"""
    lines = [
        BASHRC_BEGIN,
        "# Only when this system is cross-built on another computer (CBUILD set to",
        "# that computer); building on the system itself, or in the emulator, has",
        "# nothing to do here.",
        'if [[ -n ${CBUILD} && ${CBUILD} != "${CHOST}" ]]; then',
        "\t# Porthole's tools for this system first (compilers with --sysroot, an",
        "\t# ldd for its programs): portage ignores the caller's PATH once the",
        "\t# system's env.d sets one",
        '\texport PATH="%s:${PATH}"' % tools if tools else "\t:",
        "\t# gobject-introspection's g-ir-scanner compiles with $CC (else this",
        "\t# computer's own compiler) and runs the program it built through",
        "\t# GI_CROSS_LAUNCHER",
        '\texport CC="${CC:-${CHOST}-gcc}" CXX="${CXX:-${CHOST}-g++}"',
        '\texport GI_CROSS_LAUNCHER="%s -L %s"' % (qemu_name(chost), root)]
    body = []
    if python_moves:
        body += [
            "\t\t# Python C modules get this computer's Python's ending in a cross",
            "\t\t# build (sysconfig EXT_SUFFIX); the system's Python looks for its own"]
        for mine, theirs in python_moves:
            body += [
                "\t\twhile IFS= read -r -d '' f; do",
                '\t\t\tmv -- "${f}" "${f%%%s}%s" || die' % (mine, theirs),
                "\t\tdone < <(find \"${ED}\" -type f -name '*%s' -print0)" % mine]
    if perl_moves:
        body += [
            "\t\t# perl modules go to this computer's perl folders in a cross build",
            "\t\t# (its perl -V:install*); the system's perl looks in its own"]
        for here, theirs in perl_moves:
            body += [
                '\t\tif [[ -d ${ED}%s && ! -L ${ED}%s ]]; then' % (here, here),
                '\t\t\tmkdir -p "${ED}%s" || die' % theirs,
                '\t\t\tcp -a "${ED}%s"/. "${ED}%s"/ && rm -r "${ED}%s" || die'
                % (here, theirs, here),
                "\t\tfi"]
        body += [
            "\t\twhile IFS= read -r -d '' f; do",
            '\t\t\tsed -i %s "${f}" || die' % " ".join(
                "-e 's:^%s/:%s/:'" % m for m in perl_moves),
            "\t\tdone < <(find \"${ED}\" -type f -name .packlist -print0)"]
    if body:
        lines += [
            "\t# Fixed right after the package installs into its image, so binary",
            "\t# packages are made with the system's names; and again when a",
            "\t# package is merged, for binary packages made before this",
            "\tporthole_cross_names() {",
            "\t\tlocal d f"] + body + [
            "\t}",
            "\tpost_src_install() { porthole_cross_names; }",
            "\tpre_pkg_preinst() { porthole_cross_names; }"]
    lines += ["fi", BASHRC_END]
    return "\n".join(lines)


def write_bashrc(target, chost, base=None):
    """Porthole's block in the system's etc/portage/bashrc (portage reads it
    for the system's own packages only, not for tools built here)"""
    path = os.path.join(target.portage_dir, "bashrc")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        text = ""
    root = os.path.realpath(target.root)
    block = bashrc_block(chost, root, wrappers_dir(target, base), python_suffix_moves(root),
                         perl_dir_moves(host_perl_dirs(), system_perl_dirs(root)))
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
    # QEMU_LD_PREFIX: with binfmt on during a cross build, a program it runs
    # that was built for the system (help2man on a fresh binary, code
    # generators, tests) runs through qemu with the system's own libraries
    return {"ROOT": root, "SYSROOT": root, "PORTAGE_CONFIGROOT": os.path.realpath(
                target.config_root), "CBUILD": host_chost() or "", "CHOST": chost,
            "PATH": wrappers_dir(target, base) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
            "QEMU_LD_PREFIX": root}


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
    # --usepkg: what the system's own package folder already has (a build
    # started again, or a new system with the same settings) is installed
    # from it instead of built again; portage only takes a package whose
    # USE flags still match
    common = ["emerge", "--verbose", "--color=y", "--usepkg"]
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
    side, section, headed = "root", None, False
    for raw in text.splitlines():
        line = raw.rstrip()
        match = re.match(r"^\[(ebuild|binary)[^\]]*\]\s+(\S+)", line)
        if match:
            cpv = re.sub(r"::.*$", "", match.group(2))
            (out["root"] if " to %s/" % root in line else out["host"]).append(cpv)
            continue
        match = re.match(r"^For (/\S*):$", line)
        if match:
            headed = True
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
    # changes printed without a "For <config dir>:" heading (emerge leaves
    # it out when only one side needs any): the system's when the package
    # is merged into it, else this computer's (a build tool that runs here)
    if not headed:
        merged = {_cp(c) for c in out["root"]}
        for kind in ("use", "keywords"):
            lines = out["root_" + kind]
            out["root_" + kind] = [l for l in lines if _cp(l) in merged]
            out["host_" + kind] = [l for l in lines if _cp(l) not in merged]
    if out["host_keywords"]:
        out["ok"] = False
        out["other"].append("Packages built on this computer would need testing versions: "
                            + ", ".join(out["host_keywords"]))
    return out


def _cp(text):
    """cat/pkg of a cpv, an atom or a package.use line"""
    atom = re.sub(r"^(>=|<=|=|~|<|>)", "", text.split()[0])
    atom = re.sub(r"(:[^/]*)?(::.*)?$", "", atom)
    return re.sub(r"-\d[^/]*$", "", atom)


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
                    "# agreed to in the Build dialog, or allowed there for the rest of the build.\n"
                    "# Removed with the system.\n"
                    % (target.title, what) + "".join(l + "\n" for l in merged))
        os.chmod(path + ".new", 0o644)
        os.replace(path + ".new", path)
        written.append(path)
    return written


# what the user allowed for the rest of a build (Build dialog), kept with
# its tools: allow_python = build tools here may get the system's Python
# version added (python_targets / python_single_target), without asking
POLICY_NAME = "allow.json"
_PYTHON_FLAG = re.compile(r"^python_(single_)?targets?_[a-z0-9_]+$")


def policy_path(target, base=None):
    return os.path.join(os.path.dirname(wrappers_dir(target, base)), POLICY_NAME)


def write_policy(target, allow_python, base=None):
    import json
    path = policy_path(target, base)
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    with open(path + ".new", "w") as f:
        json.dump({"allow_python": bool(allow_python)}, f)
    os.replace(path + ".new", path)


def read_policy(target, base=None):
    import json
    try:
        with open(policy_path(target, base)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def python_only(line):
    """Whether a package.use line only adds Python targets"""
    flags = line.split()[1:]
    return bool(flags) and all(_PYTHON_FLAG.match(f) for f in flags)


def allowed_changes(parsed, policy):
    """(host_use, root_use) a build may write by itself: the system's USE
    changes (its config is Porthole's), and this computer's only when
    they just add a Python target and the user allowed that; plus what
    is left that it may not"""
    host_ok = [l for l in parsed["host_use"] if policy.get("allow_python") and python_only(l)]
    left = ([l for l in parsed["host_use"] if l not in host_ok] + parsed["root_keywords"]
            + parsed["host_keywords"] + parsed["other"])
    return host_ok, list(parsed["root_use"]), left


def apply_allowed(target, argv, env, log, portage_dir="/etc/portage", run=None):
    """Before a cross build: emerge --pretend with autounmask over argv's
    packages, and write the changes the build may make by itself
    (allowed_changes), checking again after each round: a change still
    asked for after it was written for the system is this computer's (a
    package needed in both, emerge printed it without a "For <config>:"
    heading). Returns True when nothing it can't make is left."""
    run = run or subprocess.run
    args = [a for a in argv[1:] if not a.startswith("--")]
    pretend = ["emerge", "--pretend", "--color=n", "--autounmask=y", "--autounmask-backtrack=y",
               "--autounmask-write=n"] + [a for a in argv[1:] if a in (
                   "--oneshot", "--update", "--deep", "--newuse", "--noreplace", "--usepkg",
                   "--nodeps")] + args
    policy = read_policy(target)
    written_root = set()
    left = []
    for _round in range(3):
        result = run(pretend, env=dict(os.environ, **env), capture_output=True, text=True)
        parsed = parse_pretend(result.stdout + "\n" + result.stderr, target.root)
        # written for the system already and still asked for: this computer's
        again = [l for l in parsed["root_use"] if l in written_root]
        parsed["root_use"] = [l for l in parsed["root_use"] if l not in written_root]
        parsed["host_use"] = parsed["host_use"] + [l for l in again
                                                   if l not in parsed["host_use"]]
        host_ok, root_ok, left = allowed_changes(parsed, policy)
        if not host_ok and not root_ok:
            break
        for path in write_changes(target, host_ok, root_ok, [], portage_dir):
            log("Wrote the changes this build needs in %s:" % path)
        for line in host_ok + root_ok:
            log("   " + line)
        written_root.update(root_ok)
    for line in left:
        log("!! Not changed by itself (needs you): " + line)
    return not left


def remove_host_changes(target, portage_dir="/etc/portage"):
    path = os.path.join(portage_dir, "package.use", HOST_FILE % target.name)
    if os.path.exists(path):
        os.unlink(path)
        return path
    return None
