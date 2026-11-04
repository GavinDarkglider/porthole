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


def python_multiarch(root):
    """The system's Python platform tag (aarch64-linux-gnu), from its own
    _sysconfigdata file, or None (no Python in it yet)"""
    import glob
    for path in sorted(glob.glob(os.path.join(root, "usr/lib*/python3.*",
                                              "_sysconfigdata_*_linux_*.py"))):
        return os.path.basename(path)[:-3].split("_linux_", 1)[1]
    return None


def host_multiarch():
    import sysconfig
    return sysconfig.get_config_var("MULTIARCH") or None


def misnamed_python_modules(root, host=None, target=None):
    """[(path, correct path)] of Python C modules in the system named with
    this computer's platform tag (a cross build took the suffix from this
    computer's Python): the system's Python doesn't find them"""
    host = host or host_multiarch()
    target = target or python_multiarch(root)
    if not host or not target or host == target:
        return []
    found = []
    suffix = "-%s.so" % host
    for top in ("usr/lib", "usr/lib64"):
        for folder, _dirs, files in os.walk(os.path.join(root, top)):
            for name in files:
                if ".cpython-3" in name and name.endswith(suffix):
                    path = os.path.join(folder, name)
                    found.append((path, path[:-len(suffix)] + "-%s.so" % target))
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


def perl_arch(root):
    """The system's perl architecture folder name (aarch64-linux): the one
    with its Config.pm, or None (no perl in it yet)"""
    import glob
    for path in sorted(glob.glob(os.path.join(root, "usr/lib*/perl5/*/*/Config.pm"))):
        return os.path.basename(os.path.dirname(path))
    return None


def host_perl_arch():
    try:
        return subprocess.run(["perl", "-MConfig", "-e", "print $Config{archname}"],
                              capture_output=True, text=True, timeout=30).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def repair_perl_modules(target, log=lambda m: None, host=None):
    """Perl modules already installed in this computer's perl folder
    (vendor_perl/<version>/x86_64-linux) by cross builds: moved into the
    system's own (aarch64-linux), with a link left at the old place so the
    packages' file lists stay valid. Also undoes a link the other way (the
    system's folder pointing at this computer's), keeping what was set
    aside next to it (<arch>.1)."""
    import glob
    root = os.path.realpath(target.root)
    host = host or host_perl_arch()
    arch = perl_arch(root)
    if not host or not arch or host == arch:
        return 0
    moved = 0
    for top in glob.glob(os.path.join(root, "usr/lib*/perl5/*/*")):
        wrong, right = os.path.join(top, host), os.path.join(top, arch)
        if not os.path.isdir(top) or not os.path.lexists(wrong) or os.path.islink(wrong):
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
                        f.write(text.replace("/%s/" % host, "/%s/" % arch))
                moved += 1
        shutil.rmtree(wrong)
        os.symlink(arch, wrong)
    if moved:
        log("Moved %d perl module file%s from this computer's perl folder (%s) to the "
            "system's (%s); a cross build put them there" % (
                moved, "" if moved == 1 else "s", host, arch))
    return moved


BASHRC_BEGIN = "# --- Porthole: cross builds (changed by Porthole) ---"
BASHRC_END = "# --- end of Porthole's cross build settings ---"


def bashrc_block(chost, root, tools=None, host_tag=None, target_tag=None,
                 host_perl=None, target_perl=None):
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
    if host_tag and target_tag and host_tag != target_tag:
        body += [
            "\t\t# Python C modules get this computer's platform name in a cross",
            "\t\t# build (%s); the system's Python looks for its own (%s)"
            % (host_tag, target_tag),
            "\t\twhile IFS= read -r -d '' f; do",
            '\t\t\tmv -- "${f}" "${f%%-%s.so}-%s.so" || die' % (host_tag, target_tag),
            "\t\tdone < <(find \"${ED}\" -type f -name '*.cpython-3*-%s.so' -print0)"
            % host_tag]
    if host_perl and target_perl and host_perl != target_perl:
        body += [
            "\t\t# perl modules with C code go to this computer's perl folder (%s)"
            % host_perl,
            "\t\t# in a cross build; the system's perl looks in its own (%s)" % target_perl,
            "\t\tfor d in \"${ED}\"/usr/lib*/perl5/{vendor_perl,site_perl}/*/%s; do"
            % host_perl,
            "\t\t\t[[ -d ${d} && ! -L ${d} ]] || continue",
            '\t\t\tmkdir -p "${d%%/*}/%s" || die' % target_perl,
            '\t\t\tcp -a "${d}"/. "${d%%/*}/%s"/ && rm -r "${d}" || die' % target_perl,
            "\t\tdone",
            "\t\twhile IFS= read -r -d '' f; do",
            '\t\t\tsed -i "s:/%s/:/%s/:g" "${f}" || die' % (host_perl, target_perl),
            "\t\tdone < <(find \"${ED}\" -type f -name .packlist -print0)"]
    if body:
        lines += [
            "\t# Fixed when the package is merged, so binary packages made before",
            "\t# (with the wrong names) are fixed as they are installed too",
            "\tpre_pkg_preinst() {",
            "\t\tlocal d f"] + body + ["\t}"]
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
    block = bashrc_block(chost, root, wrappers_dir(target, base), host_multiarch(),
                         python_multiarch(root), host_perl_arch(), perl_arch(root))
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
