"""
    Porthole update analyzer

    Works out a world update the way a person would: resolve it, collect
    every config change portage needs, explain why each is needed, then
    prove the proposed changes work by resolving again against a scratch
    copy of /etc/portage with autounmask disabled. Circular dependencies
    get a staged plan (temporary USE change, build, revert, rebuild).

    Nothing here builds packages or writes to /etc/portage.

    Usage:
        python3 -m porthole.updater.analyze [--json FILE] [--config-root DIR]
                                            [-- emerge targets/options]

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import argparse
import glob
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from functools import cmp_to_key

from portage.dep import Atom
from portage.versions import vercmp

# files porthole owns inside /etc/portage/package.*/
OWNED_NAME = "zz-porthole"
TEMP_NAME = "zz-porthole-temp"

WORLD_ARGS = ["--update", "--deep", "--newuse", "--with-bdeps=y", "@world"]
# let portage search for config changes and keep backtracking past them
SOLVE_ARGS = [
    "--autounmask=y",
    "--autounmask-write=n",
    "--autounmask-backtrack=y",
    "--backtrack=50",
]
# proving a fix: no autounmask help allowed
VERIFY_ARGS = ["--autounmask=n", "--backtrack=50"]
# updated as their own stage before anything else
TOOLCHAIN = ("sys-devel/binutils", "sys-kernel/linux-headers", "sys-libs/glibc",
             "sys-libs/musl", "sys-devel/gcc")
# programs still linked to libraries an update replaced
PRESERVED_ARGS = ["--oneshot", "@preserved-rebuild"]
# rounds of removing what holds the update back
MAX_REMOVAL_ROUNDS = 3
# give up after breaking this many cycles in a row
MAX_CYCLES = 8
# suggestions to try on one cycle before giving up on it
MAX_TRIES_PER_CYCLE = 6

CONFIG_DIRS = {
    "keywords": "package.accept_keywords",
    "use": "package.use",
    "unmask": "package.unmask",
    "licenses": "package.license",
    # only from the user: "keep this version" in Advanced Emerge
    "mask": "package.mask",
}

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_worker(args, config_root="/", timeout=1800):
    """Run one resolution in a child process and return its result dict"""
    env = dict(os.environ)
    env["PORTAGE_CONFIGROOT"] = config_root
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (_PKG_ROOT, env.get("PYTHONPATH")) if p
    )
    fd, out = tempfile.mkstemp(prefix="porthole-resolve-", suffix=".json")
    os.close(fd)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "porthole.updater.resolve", out] + list(args),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        try:
            with open(out) as f:
                result = json.load(f)
        except (OSError, ValueError):
            result = {"error": "resolver produced no result", "traceback": proc.stdout[-4000:]}
    finally:
        os.unlink(out)
    return result


class ScratchConfig:
    """A throwaway copy of a portage config root to test changes in"""

    def __init__(self, config_root="/"):
        self.source = os.path.join(config_root, "etc", "portage")
        self.root = tempfile.mkdtemp(prefix="porthole-config-")
        self.portage_dir = os.path.join(self.root, "etc", "portage")

    def __enter__(self):
        # gnupg/ is the sync signing keyring: private, and not needed to resolve
        shutil.copytree(self.source, self.portage_dir, symlinks=True,
                        ignore=lambda d, names: ["gnupg"] if d == self.source else [])
        # relative links (make.profile) must keep pointing at the real targets
        for dirpath, dirnames, filenames in os.walk(self.portage_dir):
            for name in dirnames + filenames:
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    target = os.readlink(path)
                    if not os.path.isabs(target):
                        original = os.path.join(
                            self.source, os.path.relpath(dirpath, self.portage_dir)
                        )
                        os.unlink(path)
                        os.symlink(
                            os.path.normpath(os.path.join(original, target)), path
                        )
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)

    def add_lines(self, kind, lines, name=OWNED_NAME):
        """Append lines to package.<kind>/<name>, converting a file to a dir"""
        path = os.path.join(self.portage_dir, CONFIG_DIRS[kind])
        if os.path.isfile(path):
            os.rename(path, path + ".orig")
            os.mkdir(path)
            os.rename(path + ".orig", os.path.join(path, "00-original"))
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, name), "a") as f:
            for line in lines:
                f.write(line + "\n")


def read_config_atoms(kind, config_root="/"):
    """Map cp -> [(line, file)] for a package.* file or directory"""
    path = os.path.join(config_root, "etc", "portage", CONFIG_DIRS[kind])
    files = []
    if os.path.isdir(path):
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            files.extend(
                os.path.join(dirpath, f)
                for f in sorted(filenames)
                if not f.startswith(".") and not f.endswith("~")
            )
    elif os.path.isfile(path):
        files.append(path)
    found = {}
    for filename in files:
        try:
            with open(filename, errors="replace") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    try:
                        atom = Atom(line.split()[0], allow_wildcard=True)
                    except Exception:
                        continue
                    found.setdefault(atom.cp, []).append((line, filename))
        except OSError:
            pass
    return found


def news_unread():
    try:
        out = subprocess.run(
            ["eselect", "news", "count", "new"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60,
        ).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def kernel_config():
    """The running kernel's options as {name: value}, or None"""
    release = os.uname().release
    for path in ("/proc/config.gz", "/boot/config-" + release):
        try:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        config = {}
        for line in lines:
            if line.startswith("CONFIG_") and "=" in line:
                name, value = line.split("=", 1)
                config[name] = value
            elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
                config[line[2:-len(" is not set")]] = "n"
        return config
    return None


def ia32_enabled():
    """True if the running kernel runs 32-bit x86 programs, None if unknown"""
    config = kernel_config()
    if config is None:
        return None
    if config.get("CONFIG_IA32_EMULATION") != "y":
        return False
    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read().split()
    except OSError:
        cmdline = []
    if "ia32_emulation=0" in cmdline:
        return False
    if config.get("CONFIG_IA32_EMULATION_DEFAULT_DISABLED") == "y":
        return "ia32_emulation=1" in cmdline
    return True


def fstab_entry(mountpoint, fstab="/etc/fstab"):
    """(device, type, options) of a mount point in fstab, or None"""
    try:
        with open(fstab) as f:
            for line in f:
                fields = line.split("#", 1)[0].split()
                if len(fields) >= 3 and fields[1].rstrip("/") == mountpoint:
                    options = fields[3].split(",") if len(fields) > 3 else []
                    return fields[0], fields[2], options
    except OSError:
        pass
    return None


def known_fstype(fstype):
    """True if the kernel has, or can load, this filesystem type"""
    if fstype in ("auto", "none", "swap"):
        return True
    try:
        with open("/proc/filesystems") as f:
            if fstype in (line.split()[-1] for line in f if line.strip()):
                return True
    except OSError:
        return True  # can't tell
    pattern = "/lib/modules/%s/**/%s.ko*" % (os.uname().release, fstype)
    return bool(glob.glob(pattern, recursive=True))


def _cp(cpv):
    """cat/pkg of a cpv, or of an atom (a command-line target can be a
    parent: games-fps/eduke32)"""
    for text in ("=" + cpv, cpv):
        try:
            return Atom(text).cp
        except Exception:
            pass
    return cpv


def _short_atom(atom):
    """Drop USE dependencies ([abi_x86_32(-)?,...]) for display"""
    return atom.split("[", 1)[0]


def _limit(items, n=5):
    items = sorted(items)
    if len(items) <= n:
        return ", ".join(items)
    return "%s and %d more" % (", ".join(items[:n]), len(items) - n)


# USE_EXPAND flags that select an implementation; flipping these tends to
# move a cycle to another slot rather than break it
_EXPAND_PREFIXES = ("python_targets_", "python_single_target_", "lua_targets_",
                    "lua_single_target_", "ruby_targets_", "llvm_slot_", "abi_")


def _cycle_key(cycle):
    """Identify a cycle by package names, ignoring versions/slots"""
    return frozenset(_cp(cpv) for cpv in cycle)


def _same_cycle(cycle, seen):
    """A cycle sharing two or more packages with one already seen has
    only moved, it wasn't broken"""
    key = _cycle_key(cycle)
    return any(len(key & other) >= 2 for other in seen)


def _solution_cost(solution):
    """Prefer turning a feature off over swapping implementations"""
    changes = solution["changes"]
    expand = sum(1 for f in changes if f.startswith(_EXPAND_PREFIXES))
    enables = sum(1 for on in changes.values() if on)
    return (expand, enables, len(changes))


def normalize_choices(choices):
    """The user's standing choices about updates (choices.json):
        exclude       packages not to update ("cat/pkg")
        reject_fixes  config lines not to add
        decline       approval stages not to run (their "key")
        accept        optional stages to run (their "key")"""
    choices = choices or {}
    return {key: sorted({str(v) for v in choices.get(key) or []})
            for key in ("exclude", "reject_fixes", "decline", "accept")}


def _atom_cp(atom):
    try:
        return Atom(atom.lstrip("!")).cp
    except Exception:
        return None


def _badness(result):
    """Lower is better: a failed resolution, then each kind of trouble"""
    return (not result.get("success"),
            len(result.get("slot_conflicts") or []) + len(result.get("blockers") or [])
            + bool(result.get("circular")) + len(result.get("unsatisfied") or []),
            len(result.get("missed_updates") or []))


def _is_exact_pin(atom):
    """True for atoms that pin one version: =cat/pkg-1.0 or ~cat/pkg-1.0"""
    return atom.lstrip("!").startswith(("=", "~"))


class Analyzer:
    def __init__(self, targets=None, config_root="/", log=None, choices=None,
                 live=False, live_exclude=(), requested=()):
        self.choices = normalize_choices(choices)
        # config lines the user chose (e.g. in Advanced Emerge):
        # [{"kind": "use"|"keywords", "line": "cat/pkg flag -flag"}]
        self.requested = [dict(r) for r in requested]
        # check live (-9999) packages for new upstream commits
        self.live = live
        self.live_exclude = set(live_exclude)
        # packages held at their version: unticked, or needing a rejected fix
        self.held = {cp: ("you chose not to update it", {"list": "exclude", "value": cp})
                     for cp in self.choices["exclude"]}
        self.base_targets = list(targets or WORLD_ARGS)
        self.targets = self.base_targets + self.exclude_args()
        self.config_root = config_root
        # installed packages the plan removes (from world, or entirely);
        # dry runs then resolve against a scratch ROOT without them
        self.removed = []
        self.scratch_root = None
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    def exclude_args(self):
        return ["--exclude=" + cp for cp in sorted(self.held)]

    def hold(self, cp, why, undo):
        """Keep a package at its installed version; undo names the choice
        that caused it ({"list": choices key, "value": entry})"""
        if cp not in self.held:
            self.held[cp] = (why, undo)
            self.targets = self.base_targets + self.exclude_args()

    # -- fixes -----------------------------------------------------------

    def build_fixes(self, needed, user_keywords):
        """Turn portage's needed changes into porthole-file lines"""
        fixes = []
        # installing: accept testing versions of the package from now on,
        # rather than this one version, which would block its updates
        install = "@world" not in self.base_targets
        for pkg in needed.get("keywords", []):
            exact = "=%s %s" % (pkg["cpv"], pkg["keyword"])
            whole = "%s %s" % (pkg["cp"], pkg["keyword"])
            fixes.append({
                "kind": "keywords",
                "cpv": pkg["cpv"],
                "line": whole if install else exact,
                # a package pinned to a keyworded sibling should follow it
                "alt_line": exact if install else whole,
            })
        for pkg in needed.get("use", []):
            flags = " ".join(
                (flag if state else "-" + flag)
                for flag, state in sorted(pkg["changes"].items())
            )
            fixes.append({
                "kind": "use",
                "cpv": pkg["cpv"],
                "line": ">=%s %s" % (pkg["cpv"], flags),
                "parents": pkg.get("parents", []),
            })
        for pkg in needed.get("unmask", []):
            fixes.append({"kind": "unmask", "cpv": pkg["cpv"], "line": "=" + pkg["cpv"]})
        for pkg in needed.get("licenses", []):
            fixes.append({
                "kind": "licenses",
                "cpv": pkg["cpv"],
                "line": "=%s %s" % (pkg["cpv"], " ".join(pkg["licenses"])),
            })
        return fixes

    def explain(self, fix, result, user_keywords):
        """Say in plain words why portage wants this change"""
        merges = {m["cpv"]: m for m in result.get("merge_list") or []}
        world_pkgs = self.wanted_by_world(fix["cpv"], merges)
        lines = []

        if fix["kind"] == "keywords":
            cp = _cp(fix["cpv"])
            # lockstep: this package pins a sibling the user already keywords
            for m in merges.values():
                for p in m.get("parents", []):
                    if p["parent"] != fix["cpv"] or not _is_exact_pin(p["atom"]):
                        continue
                    if m["cp"] in user_keywords:
                        line, filename = user_keywords[m["cp"]][0]
                        lines.append(
                            "%s is version-locked to %s (%s), and you already "
                            "accept testing versions of %s (\"%s\" in %s). "
                            "It has to move with it."
                            % (cp, m["cp"], _short_atom(p["atom"]), m["cp"], line, filename)
                        )
                        fix["lockstep"] = m["cp"]
                        if not Atom(line.split()[0], allow_wildcard=True).operator:
                            # match the user's unversioned style for siblings
                            fix["line"], fix["alt_line"] = fix["alt_line"], fix["line"]
            # or something keyworded pins this package
            pkg = merges.get(fix["cpv"], {})
            for p in pkg.get("parents", []):
                parent_cp = _cp(p["parent"]) if "/" in p["parent"] else None
                if parent_cp and _is_exact_pin(p["atom"]) and parent_cp in user_keywords:
                    lines.append(
                        "%s needs exactly this version (%s), and you already "
                        "accept testing versions of %s."
                        % (p["parent"], _short_atom(p["atom"]), parent_cp)
                    )
                    fix["lockstep"] = parent_cp
            # the installed version can't live with something else updating
            for c in (pkg.get("old_conflicts") or [])[:1]:
                wanted = self.wanted_by_world(c["with"], merges) - {c["with"]}
                lines.append(
                    "The installed %s requires %s, but this update brings %s%s. %s works "
                    "with it, and it is only in testing (%s)."
                    % (c["old"], c["atom"], c["with"],
                       " (for %s)" % _limit(wanted) if wanted else "", fix["cpv"],
                       fix["line"].split()[-1]))
            if not lines:
                lines.append("Only a testing (%s) version satisfies this."
                             % fix["line"].split()[-1])
        elif fix["kind"] == "use":
            for p in fix.get("parents", []):
                lines.append("%s requires %s." % (p["parent"], p["atom"]))
        elif fix["kind"] == "unmask":
            lines.append("The required version is masked by package.mask.")
        elif fix["kind"] == "licenses":
            lines.append("The required version uses a license you don't accept yet.")

        fix["needed_for"] = sorted(world_pkgs)
        # "needed for itself" says nothing
        if world_pkgs and world_pkgs != {fix["cpv"]}:
            lines.append("Needed for: " + _limit(world_pkgs))
        fix["explanation"] = lines
        return fix

    def wanted_by_world(self, cpv, merges, seen=None):
        """Walk parent links up to the packages in your world set"""
        seen = seen if seen is not None else set()
        if cpv in seen:
            return set()
        seen.add(cpv)
        if cpv not in merges:
            # an installed package that isn't changing
            return {cpv}
        tops = set()
        for p in merges[cpv].get("parents", []):
            if "/" not in p["parent"] or p["parent"].startswith("@"):
                tops.add(cpv)  # pulled in by a set such as @selected
            else:
                tops |= self.wanted_by_world(p["parent"], merges, seen)
        return tops

    # -- verification and circular dependencies ---------------------------

    def verify(self, fixes, temp_use=(), args=None):
        """Resolve the targets with only our changes applied, no autounmask"""
        with ScratchConfig(self.config_root) as scratch:
            for fix in fixes:
                scratch.add_lines(fix["kind"], [fix["line"]])
            if temp_use:
                scratch.add_lines("use", list(temp_use), TEMP_NAME)
            return run_worker(VERIFY_ARGS + self.world_args(args or self.targets),
                              scratch.root)

    def break_cycles(self, fixes, check):
        """Break cycles one at a time with portage's USE suggestions.

        Breaking one cycle often uncovers the next, so a suggestion counts
        when its cycle disappears, even if another one shows up.
        Returns (temp_use_lines, cycles_broken, final_check).
        """
        temp_use, broken, seen = [], [], set()
        for _ in range(MAX_CYCLES):
            circ = check.get("circular")
            if not circ or not circ.get("cycle"):
                break
            key = _cycle_key(circ["cycle"])
            if key in seen:
                break
            seen.add(key)
            self.log("Circular dependency: " + " -> ".join(circ["cycle"]))
            next_check = None
            candidates = sorted(circ.get("solutions", []), key=_solution_cost)
            tried = set()
            while candidates and len(tried) < MAX_TRIES_PER_CYCLE:
                solution = candidates.pop(0)
                pkg = solution.get("package") or solution["breaks_at"]
                flags = " ".join(
                    (f if on else "-" + f) for f, on in sorted(solution["changes"].items())
                )
                line = "=%s %s" % (pkg, flags)
                if line in tried:
                    continue
                tried.add(line)
                self.log("  trying temporary USE: " + line)
                result = self.verify(fixes, temp_use + [line])
                new = result.get("circular")
                # gone, or replaced by a different cycle; other problems (a
                # slot conflict, say) are someone else's to solve
                if result.get("success") or not new or not _same_cycle(new["cycle"], seen):
                    self.log("    breaks it" + ("" if result.get("success") else
                                                 ", next cycle found" if new else
                                                 ", other problems remain"))
                    temp_use.append(line)
                    broken.append({"cycle": circ["cycle"], "temp_use": line, "package": pkg})
                    next_check = result
                    break
                if new and _cycle_key(new["cycle"]) != key:
                    # the change only moved the cycle (another slot, another
                    # provider); its suggestions may break the original one
                    self.log("    only moves it; trying its suggestions instead")
                    candidates = sorted(candidates + new.get("solutions", []),
                                        key=_solution_cost)
                else:
                    self.log("    does not break it")
            if next_check is None:
                break
            check = next_check
        return temp_use, broken, check

    def stages(self, temp_use, broken, verified):
        if not temp_use:
            return [{
                "title": self.stage_title(),
                "commands": [["emerge", "--keep-going"] + self.targets],
                "verified": verified,
            }]
        rebuild = sorted({"=" + b["package"] for b in broken})
        rebuilt_names = ", ".join(sorted({b["package"] for b in broken}))
        return [
            {
                "title": "Build with %d temporary USE change(s) to break the cycle(s)"
                         % len(temp_use),
                "commands": [["emerge", "--keep-going"] + self.targets],
                "verified": verified,
            },
            {
                "title": "Remove the temporary USE changes and rebuild " + rebuilt_names,
                "remove_config": TEMP_NAME,
                "commands": [["emerge", "--oneshot"] + self.options() + rebuild],
                # needs stage 1 installed before it can be resolved
                "verified": "after previous stage",
            },
        ]

    def stage_title(self):
        """'Update' for the world, 'Install ...' for packages asked for"""
        if "@world" in self.targets:
            return "Update"
        wanted = [t for t in self.base_targets if not t.startswith("-")]
        return "Install " + ", ".join(wanted) if wanted else "Update"

    def options(self):
        """emerge options from the targets, for follow-up commands"""
        return [a for a in self.targets if a.startswith("-") and a != "--oneshot"]

    def root_options(self):
        """Just the options choosing which system to work on"""
        return [a for a in self.targets if a.startswith(("--root=", "--sysroot=",
                                                         "--config-root="))]

    # -- toolchain --------------------------------------------------------

    def current_gcc(self):
        """The selected gcc-config profile, e.g. x86_64-pc-linux-gnu-16"""
        try:
            out = subprocess.run(
                ["gcc-config", "-c"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    def toolchain(self, merges, system):
        """Describe toolchain packages in the update, if any"""
        pkgs = [m for m in merges if m["cp"] in TOOLCHAIN]
        if not pkgs:
            return None
        info = {"packages": [m["cpv"] for m in pkgs],
                "libc": any(m["cp"] in ("sys-libs/glibc", "sys-libs/musl") for m in pkgs)}
        gccs = [m for m in pkgs if m["cp"] == "sys-devel/gcc"]
        if gccs:
            newest = max(gccs, key=cmp_to_key(lambda a, b: vercmp(a["version"], b["version"])))
            major = newest["version"].split(".")[0]
            current = self.current_gcc()
            current_major = current.rsplit("-", 1)[-1].split(".")[0] if current else None
            info["gcc"] = {"version": newest["version"], "major": major, "current": current}
            # same-major updates are switched by the ebuild; new majors are not
            if current_major and current_major.isdigit() and int(major) > int(current_major):
                info["gcc"]["switch_to"] = "%s-%s" % (system.get("chost"), major)
                info["gcc"]["current_major"] = current_major
        return info

    def toolchain_stages(self, tc):
        stages = [{
            "title": "Update the toolchain first: " + ", ".join(tc["packages"]),
            "commands": [["emerge", "--oneshot"] + self.options()
                         + ["=" + cpv for cpv in tc["packages"]]],
            "verified": tc.get("verified", False),
        }]
        gcc = tc.get("gcc") or {}
        if gcc.get("switch_to") and "gcc-switch" not in self.choices["decline"]:
            stages.append({
                "key": "gcc-switch",
                "title": "Switch from GCC %s to GCC %s" % (gcc["current_major"], gcc["major"]),
                "approval": True,
                "why": "Portage never switches to a new major GCC version by itself. "
                       "The stages after this one build with the new compiler.",
                "commands": [
                    ["gcc-config", gcc["switch_to"]],
                    ["env-update"],
                    ["emerge", "--oneshot", "dev-build/libtool"],
                ],
                "verified": "after previous stage",
            })
        return stages

    def toolchain_notes(self, tc, system):
        notes, full_rebuild = [], None
        if tc.get("libc"):
            notes.append("The C library is being updated. Programs that are already running "
                         "keep using the old one until restarted, so reboot when convenient.")
        gcc = tc.get("gcc") or {}
        if gcc.get("switch_to"):
            modules = system.get("module_packages") or []
            if modules:
                notes.append(
                    "These packages install kernel modules: %s. Kernel modules must be "
                    "built with the same compiler as the kernel, so after switching GCC "
                    "rebuild your kernel, then run: emerge @module-rebuild"
                    % ", ".join(modules))
            notes.append("Once everything works with GCC %s you can remove GCC %s: "
                         "emerge --depclean sys-devel/gcc:%s"
                         % (gcc["major"], gcc["current_major"], gcc["current_major"]))
            flags = " ".join((system.get("build_flags") or {}).values())
            lto = "-flto" in flags
            full_rebuild = {
                "recommended": lto,
                "reason": (
                    "Your build flags use -flto. LTO objects in static libraries built "
                    "by GCC %s can't be read by GCC %s, so rebuilding everything is "
                    "recommended." % (gcc["current_major"], gcc["major"])
                    if lto else
                    "Not required: packages built with GCC %s keep working. Only worth it "
                    "if you want everything built by the new compiler." % gcc["current_major"]
                ),
                "installed_count": system.get("installed_count"),
                "commands": [["emerge", "--emptytree", "--keep-going"] + self.options() + ["@world"]],
            }
        return notes, full_rebuild

    # -- old libraries ----------------------------------------------------

    def preserved(self, fixes, temp_use, system):
        """What @preserved-rebuild would rebuild right now, or None"""
        libs = system.get("preserved_libs")
        if not libs:
            return None
        self.log("%d package(s) have old libraries kept for programs still "
                 "using them; resolving @preserved-rebuild..." % len(libs))
        check = self.verify(fixes, temp_use, self.root_options() + PRESERVED_ARGS)
        return {
            "libs": libs,
            "merge_list": check.get("merge_list") or [],
            "verified": bool(check.get("success")),
            "check": check,
        }

    def preserved_stage(self, preserved, after_update):
        """Rebuild what still links to replaced libraries. Updates often
        leave some (a library bumps its soname), so this follows every
        world update; emerge does nothing if the set is empty."""
        names = sorted({m["cp"] for m in (preserved or {}).get("merge_list") or []})
        if names:
            title = "Rebuild programs that still use old libraries: " + ", ".join(names)
        else:
            title = "Rebuild anything the update leaves using old libraries"
        return {
            "title": title,
            "commands": [["emerge", "--keep-going"] + self.root_options() + PRESERVED_ARGS],
            # the update changes which libraries are kept
            "verified": "after previous stage" if after_update
                        else bool(preserved and preserved["verified"]),
        }

    # -- live packages ------------------------------------------------------

    def live_changes(self, fixes, temp_use):
        """Live packages whose upstream has new commits, from
        smart-live-rebuild. It only asks the remotes (--pretend
        --remote-only), so it runs unprivileged. None when not checked."""
        if not self.live or self.target_root() != "/":
            return None
        slr = shutil.which("smart-live-rebuild")
        if not slr:
            return {"changed": [], "error": "smart-live-rebuild isn't installed "
                    "(app-portage/smart-live-rebuild)", "verified": True}
        command = [slr, "--pretend", "--no-color", "--remote-only",
                   "--unprivileged-user", "--quiet"]
        if os.geteuid() == 0:
            # it only reads: no need for root (and its git calls run as portage)
            command = ["runuser", "-u", "portage", "--", "env",
                       "HOME=/var/lib/portage/home"] + command
        self.log("Checking live packages for new upstream commits...")
        try:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=600, cwd="/")
        except (OSError, subprocess.SubprocessError) as e:
            return {"changed": [], "error": str(e), "verified": True}
        if proc.returncode != 0:
            return {"changed": [], "verified": True,
                    "error": (proc.stderr.strip().splitlines() or ["failed"])[-1]}
        import portage
        vardb = portage.db[portage.root]["vartree"].dbapi
        changed = []
        for line in proc.stdout.split():
            try:
                atom = Atom(line)
            except Exception:
                continue
            installed = vardb.match(atom)
            if not installed or atom.cp in self.live_exclude or atom.cp in self.held:
                continue
            changed.append({"atom": line, "cp": atom.cp, "cpv": str(installed[-1])})
        live = {"changed": changed, "error": None, "verified": True}
        if changed:
            self.log("  changed upstream: " + ", ".join(c["cpv"] for c in changed))
            check = self.verify(fixes, temp_use, ["--oneshot"] + self.root_options()
                                + ["=" + c["cpv"] for c in changed])
            live["verified"] = bool(check.get("success"))
        return live

    def live_stage(self, live, after_update):
        names = ", ".join(c["cp"] for c in live["changed"])
        return {
            "title": "Rebuild live packages with new upstream commits: " + names,
            "commands": [["emerge", "--oneshot", "--keep-going"] + self.root_options()
                         + ["=" + c["cpv"] for c in live["changed"]]],
            "verified": "after previous stage" if after_update else live["verified"],
        }

    # -- checks on the system itself ---------------------------------------

    def system_checks(self, merges, system):
        """Problems outside portage that would make the update fail.
        Each is {"severity": "error"|"warning", "title", "detail", "fix"}."""
        checks = []

        # 32-bit builds run their own test programs; without IA32
        # emulation they fail with "Exec format error"
        # multilib profiles turn abi_x86_32 on per package, as dependencies need it
        if "x86" in (system.get("multilib_abis") or []) and ia32_enabled() is False:
            needs32 = sorted({m["cp"] for m in merges if m.get("abi_x86_32")})
            checks.append({
                "severity": "error" if needs32 else "warning",
                "title": "The running kernel can't run 32-bit programs",
                "detail": (
                    "Your profile builds 32-bit (abi_x86_32) libraries, but the kernel "
                    "has IA32 emulation turned off. %s"
                    % ("These packages in the update build 32-bit code and will fail "
                       "with \"Exec format error\": %s." % _limit(needs32)
                       if needs32 else
                       "Nothing in this update needs it, but future 32-bit builds "
                       "will fail with \"Exec format error\".")),
                "fix": "Enable CONFIG_IA32_EMULATION (and leave "
                       "CONFIG_IA32_EMULATION_DEFAULT_DISABLED off, or boot with "
                       "ia32_emulation=1), rebuild the kernel and reboot.",
            })

        # kernels and boot loaders install into /boot
        entry = fstab_entry("/boot")
        if entry and "noauto" not in entry[2] and not os.path.ismount("/boot"):
            device, fstype, _options = entry
            boot_pkgs = sorted({m["cp"] for m in merges
                                if m["cp"].startswith("sys-boot/")
                                or m["cp"] in ("sys-kernel/installkernel",)
                                or m["cp"].startswith("sys-kernel/")
                                and m["cp"].split("/")[1].endswith(("-kernel", "-kernel-bin"))})
            detail = ("/etc/fstab lists %s on /boot, but nothing is mounted there, so "
                      "kernels and boot loader files are written to the root filesystem "
                      "where the boot loader won't find them." % device)
            fix = "Mount it (mount /boot) and fix whatever stopped it mounting at boot."
            if not known_fstype(fstype):
                detail += " Its filesystem type \"%s\" isn't one the kernel knows." % fstype
                if fstype.lower() in ("fat32", "fat16", "fat"):
                    fix = ("Change the type of /boot in /etc/fstab from \"%s\" to "
                           "\"vfat\", then mount /boot." % fstype)
            if boot_pkgs:
                detail += " This update installs: %s." % ", ".join(boot_pkgs)
            checks.append({
                "severity": "error" if boot_pkgs else "warning",
                "title": "/boot is not mounted",
                "detail": detail,
                "fix": fix,
            })
        return checks

    # -- updates portage leaves out ----------------------------------------

    def target_root(self):
        for arg in self.targets:
            if arg.startswith("--root="):
                return arg.split("=", 1)[1]
        return "/"

    def world_entries(self):
        """The world file and world_sets, as emerge reads them"""
        entries = []
        base = os.path.join(self.target_root(), "var", "lib", "portage")
        for name in ("world", "world_sets"):
            try:
                with open(os.path.join(base, name)) as f:
                    entries.extend(line.strip() for line in f
                                   if line.strip() and not line.startswith("#"))
            except OSError:
                pass
        return entries

    def without_removed(self):
        """A scratch ROOT that looks like the target system without the
        packages being removed: its vdb links to the real entries, its
        world file leaves them out. Resolving against it is a dry run of
        the removal; nothing real is touched."""
        self.drop_scratch_root()
        if not self.removed:
            return
        real = self.target_root()
        # a soft-blocked package is only taken out of world; the update
        # itself uninstalls it, so it stays installed in the dry run
        removed_cpvs = {r["cpv"] for r in self.removed
                        if r["kind"] == "orphan" or r.get("hard")}
        removed_cps = {r["cp"] for r in self.removed}
        scratch = tempfile.mkdtemp(prefix="porthole-root-")
        vdb = os.path.join(real, "var", "db", "pkg")
        for cat in os.listdir(vdb):
            if not os.path.isdir(os.path.join(vdb, cat)):
                continue
            os.makedirs(os.path.join(scratch, "var", "db", "pkg", cat))
            for pf in os.listdir(os.path.join(vdb, cat)):
                if "%s/%s" % (cat, pf) not in removed_cpvs:
                    os.symlink(os.path.join(vdb, cat, pf),
                               os.path.join(scratch, "var", "db", "pkg", cat, pf))
        lib = os.path.join(scratch, "var", "lib", "portage")
        os.makedirs(lib)
        for name in ("world", "world_sets"):
            try:
                with open(os.path.join(real, "var", "lib", "portage", name)) as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            with open(os.path.join(lib, name), "w") as f:
                for line in lines:
                    try:
                        if not line.startswith("@") and Atom(line.strip()).cp in removed_cps:
                            continue
                    except Exception:
                        pass
                    f.write(line + "\n")
        self.scratch_root = scratch

    def drop_scratch_root(self):
        if getattr(self, "scratch_root", None):
            shutil.rmtree(self.scratch_root, ignore_errors=True)
        self.scratch_root = None

    def world_args(self, args):
        """args aimed at the scratch ROOT while packages are being removed"""
        if not getattr(self, "scratch_root", None):
            return list(args)
        out = [a for a in args if not a.startswith(("--root=", "--sysroot="))]
        return out + ["--root=" + self.scratch_root, "--sysroot=" + self.scratch_root]

    def try_removals(self, result, check, fixes=None, temp_use=()):
        """Dry-run removing what holds the update back (found in result or
        check); keep the removals if they help. With fixes, they are judged
        by verifying with the fixes and cycle breakers in place, since a
        plain resolution may stop at a cycle before reaching the problem.
        Returns the resolution to plan from."""
        known = {r["cpv"] for r in self.removed}
        new = [r for r in self.find_removals(check) + self.find_removals(result)
               if r["cpv"] not in known]
        new = list({r["cpv"]: r for r in new}.values())
        if not new:
            return result
        self.removed += new
        self.without_removed()
        self.log("Trying without: " + ", ".join(r["cpv"] for r in new))
        without = run_worker(SOLVE_ARGS + self.world_args(self.targets), self.config_root)
        if fixes is not None:
            better = _badness(self.verify(fixes, temp_use)) < _badness(check)
        else:
            better = _badness(without) < _badness(result)
        if "merge_list" in without and better:
            held = {m["cpv"] for m in result.get("missed_updates") or []}
            freed = held - {m["cpv"] for m in without.get("missed_updates") or []}
            self.log("  that frees: " + (", ".join(sorted(freed)) or "the update"))
            return without
        self.log("  doesn't help")
        self.removed = self.removed[:-len(new)]
        self.without_removed()
        return result

    def find_removals(self, result):
        """Installed packages whose removal would unblock the update:
        orphans (no ebuild left, nothing needs them) holding updates back,
        and world packages a new version blocks"""
        dependents = result.get("dependents") or {}
        world = set()
        for entry in self.world_entries():
            try:
                world.add(Atom(entry).cp)
            except Exception:
                pass
        removals = {}
        declined, accepted = set(self.choices["decline"]), set(self.choices["accept"])
        for missed in result.get("missed_updates") or []:
            for p in missed["parents"]:
                if not p.get("installed") or dependents.get(p["parent"]):
                    continue
                cp = _cp(p["parent"])
                # orphans are proposed; a package that still has an ebuild only
                # when the user asked for it to go
                if ("remove:" + cp) in declined or (
                        p.get("in_repos") and ("remove:" + cp) not in accepted):
                    continue
                r = removals.setdefault(p["parent"], {
                    "kind": "chosen" if p.get("in_repos") else "orphan",
                    "cpv": p["parent"], "cp": cp,
                    "holds": [], "atoms": [], "in_world": cp in world})
                r["holds"].append(missed["cpv"])
                r["atoms"].append(p["atom"])
        # an installed package that can't be rebuilt, pinning one side of a
        # slot conflict (typically a subslot it was built against)
        unrebuildable = set((result.get("system") or {}).get("unrebuildable") or ())
        for conflict in result.get("slot_conflicts") or []:
            for pkg in conflict["packages"]:
                for p in pkg["parents"]:
                    if (p["parent"] not in unrebuildable or dependents.get(p["parent"])
                            or ("remove:" + _cp(p["parent"])) in declined):
                        continue
                    r = removals.setdefault(p["parent"], {
                        "kind": "orphan", "cpv": p["parent"], "cp": _cp(p["parent"]),
                        "holds": [], "atoms": [], "in_world": _cp(p["parent"]) in world})
                    # the newest version wanted is the one being held back
                    wanted = [q for q in conflict["packages"] if not q["installed"]]
                    if wanted:
                        newest = max(wanted, key=cmp_to_key(
                            lambda a, b: vercmp(a["version"], b["version"])))["cpv"]
                        if newest not in r["holds"]:
                            r["holds"].append(newest)
                    if p["atom"] not in r["atoms"]:
                        r["atoms"].append(p["atom"])
        for blocker in result.get("blockers") or []:
            for blocked in blocker["blocked"]:
                if (not blocked["installed"] or blocked["cp"] not in world
                        or dependents.get(blocked["cpv"])
                        or ("replace:" + blocked["cp"]) in declined):
                    continue
                removals.setdefault(blocked["cpv"], {
                    "kind": "replaced", "cpv": blocked["cpv"], "cp": blocked["cp"],
                    "by": [b["cpv"] for b in blocker["blocking"]],
                    "hard": blocker["atom"].startswith("!!"), "in_world": True})
        return list(removals.values())

    def removal_stages(self, verified):
        stages = []
        root = self.root_options()
        for r in self.removed:
            if r["kind"] in ("orphan", "chosen"):
                commands = ([["emerge", "--deselect"] + root + [r["cp"]]] if r["in_world"]
                            else [])
                commands.append(["emerge", "--depclean"] + root + [r["cp"]])
                orphan = r["kind"] == "orphan"
                stages.append({
                    "key": "remove:" + r["cp"],
                    "title": ("Remove %s (no longer in any repository)" if orphan
                              else "Remove %s (you chose to)") % r["cpv"],
                    "approval": True,
                    "why": "It holds back %s: it requires %s%s. Nothing else installed "
                           "needs it."
                           % (", ".join(r["holds"]), ", ".join(sorted(set(r["atoms"]))),
                              ", and it can't be updated or rebuilt because its ebuild "
                              "is gone" if orphan else ""),
                    "commands": commands,
                    "verified": verified,
                })
            else:
                commands = [["emerge", "--deselect"] + root + [r["cp"]]]
                if r["hard"]:
                    commands.append(["emerge", "--unmerge"] + root + [r["cp"]])
                stages.append({
                    "key": "replace:" + r["cp"],
                    "title": "Let %s replace %s" % (", ".join(r["by"]), r["cpv"]),
                    "approval": True,
                    "why": "%s blocks %s, which is in your world file, so portage won't "
                           "remove it. Taking it out of world lets the update replace "
                           "it. Nothing else installed needs it."
                           % (", ".join(r["by"]), r["cpv"]),
                    "commands": commands,
                    "verified": verified,
                })
        return stages

    def held_back(self, result):
        """Updates portage leaves out, explained"""
        held = []
        self.follow_on_holds = []
        held_cps = {m["cp"] for m in result.get("missed_updates") or []}
        dependents = result.get("dependents") or {}
        for missed in result.get("missed_updates") or []:
            kept = [p for p in missed["parents"] if not p.get("installed")
                    and _atom_cp(p["atom"]) in self.held]
            if kept and len(kept) == len(missed["parents"]):
                self.follow_on_holds.append({
                    "cp": missed["cp"],
                    "why": "%s needs %s, and you are keeping %s"
                           % (missed["cpv"], kept[0]["atom"], _atom_cp(kept[0]["atom"])),
                    "undo": None})
                continue
            why, options = [], []
            for p in missed["parents"]:
                atom = p["atom"]
                try:
                    atom_cp = Atom(atom.lstrip("!")).cp
                except Exception:
                    atom_cp = None
                if not p.get("installed"):
                    if atom_cp in self.held:
                        why.append("%s needs %s, and you are keeping %s at its "
                                   "current version." % (p["parent"], atom, atom_cp))
                    elif atom_cp in held_cps and atom_cp != missed["cp"]:
                        why.append("%s needs %s, which is held back too."
                                   % (p["parent"], atom))
                    else:
                        why.append("%s needs %s." % (p["parent"], atom))
                    continue
                text = "%s (installed) requires %s" % (p["parent"], atom)
                if atom.endswith("=") and ":" in atom:
                    text = ("%s was built against the current version's ABI (%s) and "
                            "has to be rebuilt to use a new one" % (p["parent"], atom))
                if not p.get("in_repos"):
                    text += ", and no repository has its ebuild any more, so it can't " \
                            "be updated or rebuilt"
                else:
                    text += "; no available version of it works with %s yet" % missed["cpv"]
                needed_by = dependents.get(p["parent"])
                if needed_by:
                    text += ". It is needed by " + _limit(needed_by)
                elif ("remove:" + _cp(p["parent"])) not in self.choices["decline"]:
                    options.append({"key": "remove:" + _cp(p["parent"]),
                                    "label": "Remove %s" % p["parent"]})
                why.append(text + ".")
            # holding it on purpose quiets it until the user changes their mind
            options.insert(0, {"key": "exclude:" + missed["cp"],
                               "label": "Keep %s" % (missed["selected"] or missed["cp"])})
            held.append({"cpv": missed["cpv"], "cp": missed["cp"],
                         "selected": missed["selected"],
                         "reason": missed["reason"], "why": why, "options": options})
        return held

    def hold_blocked_by_choice(self, check):
        """A package the user keeps can't be replaced: hold the update
        that blocks it"""
        for blocker in check.get("blockers") or []:
            for blocked in blocker["blocked"]:
                if ("replace:" + blocked["cp"]) not in self.choices["decline"]:
                    continue
                for pkg in blocker["blocking"]:
                    if not pkg["installed"]:
                        self.hold(pkg["cp"], "it would replace %s, which you chose to keep"
                                  % blocked["cp"],
                                  {"list": "decline", "value": "replace:" + blocked["cp"]})

    def plan_fixes(self, result, user_keywords):
        """Config changes the resolution needs, minus those the user
        rejected (holding what needed them, and resolving again)"""
        for _ in range(3):
            fixes = self.build_fixes(result.get("needed_changes") or {}, user_keywords)
            for fix in fixes:
                self.explain(fix, result, user_keywords)
            fixes, changed = self.reject_fixes(fixes)
            if not changed:
                break
            self.log("Holding what needs rejected changes: "
                     + ", ".join(sorted(self.held)))
            result = run_worker(SOLVE_ARGS + self.world_args(self.targets), self.config_root)
        return fixes, result

    def find_unlocks(self, result, fixes):
        """Config changes that let a held-back update through: ask for the
        held-back version itself, so autounmask says what it would take
        (typically a newer, keyworded version of what holds it back)"""
        unlocks = []
        have = {f["line"] for f in fixes}
        rejected = set(self.choices["reject_fixes"])
        for missed in (result.get("missed_updates") or [])[:5]:
            holders = [p for p in missed["parents"] if p.get("installed") and p.get("in_repos")]
            if not holders or missed["cp"] in self.held:
                continue
            self.log("Looking for what would let %s through..." % missed["cpv"])
            ask = run_worker(SOLVE_ARGS + self.world_args(self.targets)
                             + ["=" + missed["cpv"]], self.config_root)
            needed = ask.get("needed_changes") or {}
            if not ask.get("success") or not any(needed.values()):
                self.log("  nothing would")
                continue
            for fix in self.build_fixes(needed, {}):
                if fix["line"] in have or fix["line"] in rejected:
                    continue
                self.explain(fix, ask, {})
                fix["unlocks"] = missed["cpv"]
                fix["explanation"].insert(0, "Lets %s update, which %s is waiting for."
                                          % (_cp(fix["cpv"]), missed["cpv"]))
                unlocks.append(fix)
                have.add(fix["line"])
            self.log("  " + ", ".join(f["line"] for f in unlocks if f["unlocks"] == missed["cpv"]))
        return unlocks

    def reject_fixes(self, fixes):
        """Drop fixes the user rejected, holding what needed them.
        Returns the remaining fixes, and whether anything new is held."""
        rejected = set(self.choices["reject_fixes"])
        keep, changed = [], False
        for fix in fixes:
            if fix["line"] not in rejected and fix.get("alt_line") not in rejected:
                keep.append(fix)
                continue
            for cpv in fix.get("needed_for") or [fix["cpv"]]:
                cp = _cp(cpv)
                if cp not in self.held:
                    line = fix["line"] if fix["line"] in rejected else fix["alt_line"]
                    self.hold(cp, "updating it needs %s, which you rejected" % line,
                              {"list": "reject_fixes", "value": line})
                    changed = True
        return keep, changed

    # -- main entry -----------------------------------------------------

    def analyze(self):
        try:
            if not self.requested:
                return self._analyze()
            # everything is resolved with the user's chosen settings in place
            original = self.config_root
            with ScratchConfig(original) as scratch:
                for r in self.requested:
                    scratch.add_lines(r["kind"], [r["line"]])
                self.config_root = scratch.root
                try:
                    plan = self._analyze()
                finally:
                    self.config_root = original
            return self.add_requested(plan)
        finally:
            self.drop_scratch_root()

    def add_requested(self, plan):
        """The user's chosen settings are written with the other fixes"""
        if plan.get("status") == "error":
            return plan
        have = {f["line"] for f in plan["fixes"]}
        chosen = []
        for r in self.requested:
            if r["line"] in have:
                continue
            atom = r["line"].split()[0]
            try:
                cpv = Atom(atom).cpv if Atom(atom).operator else Atom(atom).cp
            except Exception:
                cpv = atom
            chosen.append({"kind": r["kind"], "cpv": cpv, "line": r["line"],
                           "requested": True, "verified": plan.get("verified", False),
                           "explanation": ["You chose this setting."]})
        plan["fixes"] = chosen + plan["fixes"]
        plan["requested"] = self.requested
        # the user's own choices don't need approving again
        if (plan["status"] == "needs-approval"
                and not [f for f in plan["fixes"] if not f.get("requested")]
                and not plan.get("cycles_broken")
                and not any(st.get("approval") for st in plan.get("stages") or [])):
            plan["status"] = "ready"
        return plan

    def _analyze(self):
        started = time.monotonic()
        user_keywords = read_config_atoms("keywords", self.config_root)

        self.log("Resolving (%s)..." % " ".join(self.targets))
        result = run_worker(SOLVE_ARGS + self.targets, self.config_root)
        if "error" in result and "merge_list" not in result:
            return {"status": "error", "error": result["error"],
                    "traceback": result.get("traceback")}
        self.log("  %d packages, %.0fs" % (len(result.get("merge_list") or []),
                                           result.get("seconds", 0)))

        result = self.try_removals(result, result)
        system = result.get("system") or {}
        fixes, result = self.plan_fixes(result, user_keywords)
        unlocks = self.find_unlocks(result, fixes)
        fixes += unlocks

        plan = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "targets": self.targets,
            "merge_list": result.get("merge_list") or [],
            "fixes": fixes,
            # eselect only knows the running system
            "news_unread": news_unread() if self.target_root() == "/" else None,
            "resolver_errors": result.get("errors") or {},
        }

        # each round ends with a verification, so the plan always matches it
        for round_ in range(MAX_REMOVAL_ROUNDS + 1):
            temp_use, broken = [], []
            if plan["merge_list"]:
                # prove it: same targets, our changes only, no autounmask
                self.log("Verifying %d proposed change(s)..." % len(fixes))
                check = self.verify(fixes)
                if not check.get("success") and check.get("circular"):
                    temp_use, broken, check = self.break_cycles(fixes, check)
            else:
                check = result  # nothing to update, nothing to prove
            if (check.get("success") and not check.get("missed_updates")
                    or round_ == MAX_REMOVAL_ROUNDS):
                break
            # what is still in the way may only show once the rest is solved
            removed_before, held_before = len(self.removed), len(self.held)
            result = self.try_removals(result, check, fixes, temp_use)
            self.hold_blocked_by_choice(check)
            if len(self.held) > held_before:
                self.log("Holding updates that would replace what you keep: "
                         + ", ".join(sorted(self.held)))
                result = run_worker(SOLVE_ARGS + self.world_args(self.targets),
                                    self.config_root)
            elif len(self.removed) == removed_before:
                break
            fixes, result = self.plan_fixes(result, user_keywords)
            fixes += [u for u in unlocks if u["line"] not in {f["line"] for f in fixes}]
            plan.update(merge_list=result.get("merge_list") or [], fixes=fixes)
        if unlocks and not check.get("success"):
            # an unlock is only worth proposing if it works
            self.log("The changes for held-back updates don't verify; leaving them out")
            fixes = [f for f in fixes if not f.get("unlocks")]
            temp_use, broken = [], []
            check = self.verify(fixes)
            if not check.get("success") and check.get("circular"):
                temp_use, broken, check = self.break_cycles(fixes, check)
            plan["fixes"] = fixes
        verified = bool(check.get("success"))
        if verified and check.get("merge_list") is not None:
            # exactly what the verified resolution will merge
            plan["merge_list"] = check["merge_list"]
        # report what the final check says is held back
        if verified:
            result = dict(result, missed_updates=check.get("missed_updates"),
                          dependents=check.get("dependents") or result.get("dependents"))

        for fix in fixes:
            fix["verified"] = verified
        plan["verified"] = verified
        plan["cycles_broken"] = broken
        stages = self.stages(temp_use, broken, verified) if plan["merge_list"] else []
        tc = self.toolchain(plan["merge_list"], system)
        plan["toolchain"] = tc
        plan["notes"], plan["full_rebuild"] = [], None
        if tc:
            if verified:
                self.log("Verifying the toolchain stage on its own...")
                check_tc = self.verify(fixes, temp_use, ["--oneshot"] + self.options()
                                       + ["=" + cpv for cpv in tc["packages"]])
                tc["verified"] = bool(check_tc.get("success"))
            if len(tc["packages"]) == len(plan["merge_list"]):
                stages = []  # nothing left after the toolchain stage
            stages = self.toolchain_stages(tc) + stages
            plan["notes"], plan["full_rebuild"] = self.toolchain_notes(tc, system)
        if temp_use:
            # the temporary USE changes are in place from the first stage on
            stages[0]["config"] = {"use": temp_use, "file": TEMP_NAME}
        # removals first: the rest was worked out without those packages
        stages = self.removal_stages(verified) + stages
        plan["removed"] = self.removed
        plan["choices"] = self.choices
        plan["held_by_you"] = [{"cp": cp, "why": why, "undo": undo}
                               for cp, (why, undo) in sorted(self.held.items())]
        plan["held_back"] = self.held_back(result)
        # held back only by what the user keeps: their decision, not a problem
        plan["held_by_you"] += self.follow_on_holds
        plan["missing_world"] = [w for w in result.get("missing_world") or []
                                 if w not in {r["cp"] for r in self.removed}]

        live = self.live_changes(fixes, temp_use)
        plan["live"] = live
        lives = (live or {}).get("changed") or []
        if lives:
            # after the world update: it may bring what they build against
            stages.append(self.live_stage(live, bool(stages)))
            if verified and not live["verified"] and not stages[:-1]:
                verified = False
        preserved = self.preserved(fixes, temp_use, system)
        rebuilds = (preserved or {}).get("merge_list") or []
        if stages or rebuilds:
            stages.append(self.preserved_stage(preserved, bool(stages)))
        if preserved:
            if verified and not preserved["verified"]:
                verified, check = False, preserved["check"]
            del preserved["check"]
        plan["verified"] = verified
        plan["preserved"] = preserved
        plan["stages"] = stages
        plan["checks"] = self.system_checks(plan["merge_list"] + rebuilds, system)
        blocked = any(c["severity"] == "error" for c in plan["checks"])
        if not verified:
            # report what is still wrong with the changes applied
            remaining = {}
            for key in ("slot_conflicts", "circular", "blockers", "unsatisfied"):
                if check.get(key):
                    remaining[key] = check[key]
            if any((check.get("needed_changes") or {}).values()):
                remaining["needed_changes"] = check["needed_changes"]
            plan["remaining"] = remaining
            plan["verify_error"] = check.get("error")

        if not plan["merge_list"] and not rebuilds and not lives and plan["held_back"]:
            plan["status"] = "needs-help"  # not up to date: updates are stuck
        elif not plan["merge_list"] and not rebuilds and not lives:
            plan["status"] = "up-to-date"
        elif not verified or blocked:
            plan["status"] = "needs-help"
        elif fixes or temp_use or any(st.get("approval") for st in stages):
            plan["status"] = "needs-approval"
        else:
            plan["status"] = "ready"
        plan["seconds"] = round(time.monotonic() - started, 1)
        return plan


# -- approving a plan made by someone else ------------------------------------

def plan_signature(plan):
    """A fingerprint of what a plan would do: the packages it merges, the
    config lines it adds and the commands it runs. A plan the user
    reviewed (worked out as the user) is carried out only if the runner,
    planning again as root, arrives at the same fingerprint. The
    old-library rebuild is left out: it changes nothing by itself, and
    only root can see the preserved libraries."""
    import hashlib
    commands = [c for st in plan.get("stages") or [] for c in st["commands"]
                if "@preserved-rebuild" not in c]
    temp = [line for st in plan.get("stages") or []
            for line in (st.get("config") or {}).get("use") or []]
    data = {
        "fixes": sorted("%s %s" % (f["kind"], f["line"]) for f in plan.get("fixes") or []),
        "merges": sorted(m["cpv"] for m in plan.get("merge_list") or []),
        "commands": commands,
        "temp_use": temp,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]


# -- text report -----------------------------------------------------------

KIND_NAMES = {"update": "U", "new": "N", "new-slot": "NS", "rebuild": "R", "downgrade": "D"}


def format_report(plan):
    out = []
    status = plan.get("status")
    if status == "error":
        return "Resolver failed: %s\n%s" % (plan["error"], plan.get("traceback") or "")
    merges = plan["merge_list"]
    rebuilds = (plan.get("preserved") or {}).get("merge_list") or []
    lives = (plan.get("live") or {}).get("changed") or []
    counts = {}
    for m in merges:
        counts[m["kind"]] = counts.get(m["kind"], 0) + 1
    if rebuilds:
        counts["old-library rebuild"] = len(rebuilds)
    if lives:
        counts["live rebuild"] = len(lives)
    out.append({
        "up-to-date": "System is up to date." + (
            " (%d package(s) kept by your choices)" % len(plan["held_by_you"])
            if plan.get("held_by_you") else ""),
        "ready": "%d updates ready to install, no changes needed." % len(merges)
                 if merges else
                 "%d package(s) need rebuilding, no changes needed."
                 % (len(rebuilds) + len(lives)),
        "needs-approval": "%d updates; %d change(s) need your approval (verified to work)."
                          % (len(merges), len(plan["fixes"]) + len(plan.get("cycles_broken") or [])
                             + sum(1 for st in plan["stages"] if st.get("approval"))),
        "needs-help": "%d updates; could not find a verified solution automatically."
                      % (len(merges) + len(rebuilds)) if not plan.get("verified", True) else
                      "%d updates; fix the problem(s) below before building."
                      % (len(merges) + len(rebuilds)) if any(
                          c["severity"] == "error" for c in plan.get("checks") or []) else
                      "%d update(s) held back; they need a decision from you."
                      % len(plan.get("held_back") or []),
    }[status])
    if counts:
        out.append("  " + ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())))
    if plan.get("news_unread"):
        out.append("  %d unread news item(s): run 'eselect news read'" % plan["news_unread"])

    for c in plan.get("checks") or []:
        out.append("\n%s: %s" % ("PROBLEM" if c["severity"] == "error" else "Warning",
                                  c["title"]))
        out.append("  " + c["detail"])
        out.append("  Fix: " + c["fix"])

    held = plan.get("held_back") or []
    if held:
        out.append("\nHeld back (portage leaves these out and still reports success):")
        for h in held:
            out.append("  %s (staying at %s)" % (h["cpv"], h["selected"] or "nothing"))
            for line in h["why"]:
                out.append("    - " + line)
            if h.get("options"):
                out.append("    choices: " + "; ".join(o["label"] for o in h["options"]))
    if plan.get("held_by_you"):
        out.append("\nHeld at their current version by your choices:")
        for h in plan["held_by_you"]:
            out.append("  %s: %s" % (h["cp"], h["why"]))
    if plan.get("missing_world"):
        out.append("\nIn your world file but no repository has them any more: "
                   + ", ".join(plan["missing_world"]))
    for r in plan.get("removed") or []:
        if r["kind"] == "orphan":
            out.append("\nRemoving %s frees: %s" % (r["cpv"], ", ".join(r["holds"])))

    preserved = plan.get("preserved")
    if preserved:
        out.append("\nOld libraries kept because installed programs still use them:")
        for owner, files in sorted(preserved["libs"].items()):
            out.append("  replaced by %s: %s" % (owner, _limit(
                {os.path.basename(f) for f in files})))
        if rebuilds:
            out.append("  rebuilding these lets portage remove them: "
                       + ", ".join(m["cpv"] for m in rebuilds))

    if plan["fixes"]:
        out.append("\nProposed changes:")
        for kind in CONFIG_DIRS:
            kind_fixes = [f for f in plan["fixes"] if f["kind"] == kind]
            if not kind_fixes:
                continue
            out.append("\n  /etc/portage/%s/%s:" % (CONFIG_DIRS[kind], OWNED_NAME))
            for fix in kind_fixes:
                out.append("    %s" % fix["line"])
                for line in fix.get("explanation", []):
                    out.append("      - " + line)
                if fix.get("lockstep"):
                    out.append("      (or for this version only: %s)" % fix["alt_line"])

    for b in plan.get("cycles_broken") or []:
        out.append("\nCircular dependency: " + " -> ".join(b["cycle"]))
        out.append("  broken with temporary USE: " + b["temp_use"])

    if plan.get("stages"):
        out.append("\nPlan:")
        for i, stage in enumerate(plan["stages"], 1):
            out.append("  %d. %s" % (i, stage["title"]))
            if stage.get("config"):
                out.append("       temporary /etc/portage/package.use/%s:" % stage["config"]["file"])
                for line in stage["config"]["use"]:
                    out.append("         " + line)
            if stage.get("approval"):
                out.append("       (needs your approval) " + stage.get("why", ""))
            for command in stage["commands"]:
                out.append("       $ " + " ".join(command))

    for note in plan.get("notes") or []:
        out.append("\nNote: " + note)
    full = plan.get("full_rebuild")
    if full:
        out.append("\nOptional full rebuild (%s, %s installed packages):"
                   % ("recommended" if full["recommended"] else "not required",
                      full.get("installed_count", "?")))
        out.append("  " + full["reason"])
        for command in full["commands"]:
            out.append("  $ " + " ".join(command))

    if status == "needs-help" and not plan.get("verified", True):
        out.append("\nStill unresolved with the changes above applied:")
        rem = plan.get("remaining", {})
        for c in rem.get("slot_conflicts") or []:
            out.append("  slot conflict in %s:" % c["slot_atom"])
            for p in c["packages"]:
                out.append("    %s pulled in by %s" % (
                    p["cpv"], ", ".join("%s (%s)" % (x["parent"], x["atom"]) for x in p["parents"])))
        cyc = rem.get("circular")
        if cyc:
            out.append("  circular dependency: " + " -> ".join(cyc["cycle"]))
            if cyc.get("solutions"):
                out.append("    portage's suggestions did not break it:")
            for sol in cyc.get("solutions") or []:
                out.append("      %s: %s" % (sol.get("package") or sol["breaks_at"], sol["changes"]))
        for b in rem.get("blockers") or []:
            blocked = ", ".join("%s%s (wanted by %s)" % (
                p["cpv"], " installed" if p["installed"] else "",
                ", ".join(x["parent"] for x in p["parents"]) or "nothing")
                for p in b.get("blocked") or []) or b["atom"]
            out.append("  %s blocks %s" % (b["parent"], blocked))
        for u in rem.get("unsatisfied") or []:
            ru = u.get("required_use")
            if ru:
                out.append("  %s can't be built with these USE flags: they break its "
                           "rules (REQUIRED_USE). Failing: %s" % (ru["cpv"], ru["failing"]))
                out.append("    flags it would get: %s" % " ".join(ru["use"]))
            else:
                out.append("  nothing satisfies %s (%s)" % (
                    u["atom"], "needed by " + u["parent"] if u["parent"] else "asked for"))
        if plan.get("verify_error"):
            out.append("  resolver error: " + plan["verify_error"])

    if merges:
        out.append("\nUpdates:")
    for m in merges:
        old = " [%s]" % ", ".join(m["replaces"]) if m["replaces"] else ""
        out.append("  [%-2s] %s%s" % (KIND_NAMES.get(m["kind"], "?"), m["cpv"], old))
    if lives:
        out.append("\nLive packages with new upstream commits:")
    for c in lives:
        out.append("  [L ] %s" % c["cpv"])
    if (plan.get("live") or {}).get("error"):
        out.append("\nCouldn't check live packages: " + plan["live"]["error"])
    if rebuilds:
        out.append("\nRebuilds for old libraries%s:"
                   % (" (current list; rerun after the update)" if merges else ""))
    for m in rebuilds:
        out.append("  [%-2s] %s" % (KIND_NAMES.get(m["kind"], "?"), m["cpv"]))
    out.append("\n(analysis took %.0fs)" % plan["seconds"])
    return "\n".join(out)


def summarize(plan):
    """(title, body) describing a plan for people"""
    merges = plan.get("merge_list") or []
    rebuilds = (plan.get("preserved") or {}).get("merge_list") or []
    lives = (plan.get("live") or {}).get("changed") or []
    status = plan.get("status")
    count = len(merges) + len(rebuilds) + len(lives)
    changes = (len(plan.get("fixes") or []) + len(plan.get("cycles_broken") or [])
               + sum(1 for s in plan.get("stages") or [] if s.get("approval")))
    problems = [c for c in plan.get("checks") or [] if c["severity"] == "error"]
    if status == "ready" and not merges:
        title = "%d package%s need%s rebuilding" % (
            count, "s" if count != 1 else "", "" if count != 1 else "s")
        body = []
        if rebuilds:
            body.append("%s still use%s old library versions an earlier update left "
                        "behind." % (", ".join(sorted({m["cp"].split("/")[1] for m in rebuilds})),
                                     "s" if len(rebuilds) == 1 else ""))
        if lives:
            body.append("New upstream commits for %s."
                        % ", ".join(c["cp"].split("/")[1] for c in lives))
    elif status == "ready":
        title = "%d updates ready to install" % count
        body = ["No configuration changes are needed."]
    elif status == "needs-approval" and plan.get("request"):
        title = "%d package%s to install" % (count, "s" if count != 1 else "")
        body = ["%d change%s need%s your approval first (already tested to work)."
                % (changes, "s" if changes != 1 else "", "" if changes != 1 else "s")]
    elif status == "needs-approval":
        title = "%d updates available" % count
        body = ["%d change%s need%s your approval first (already tested to work)."
                % (changes, "s" if changes != 1 else "", "" if changes != 1 else "s")]
    elif status == "needs-help" and problems:
        title = "%d updates need attention" % count
        body = ["Fix this first or the build will fail: %s."
                % "; ".join(c["title"] for c in problems)]
    elif status == "needs-help" and plan.get("verified", True) and plan.get("held_back"):
        held = len(plan["held_back"])
        title = "%d update%s held back" % (held, "s" if held != 1 else "")
        body = ["Something installed is in the way; the update assistant shows the options."]
    elif status == "needs-help":
        title = "%d updates need attention" % count
        body = ["A working update plan couldn't be found automatically."]
    elif status == "error":
        return "Update check failed", "Open the report for details."
    else:
        mine = len(plan.get("held_by_you") or [])
        return "System is up to date", (
            "%d package%s kept at %s version by your choices."
            % (mine, "s" if mine != 1 else "", "their" if mine != 1 else "its")
            if mine else "")
    if lives and merges:
        body.append("Includes rebuilding %d live package%s."
                    % (len(lives), "s" if len(lives) != 1 else ""))
    held = len(plan.get("held_back") or [])
    if held and status in ("ready", "needs-approval"):
        body.append("%d more update%s held back." % (held, "s are" if held != 1 else " is"))
    gcc = (plan.get("toolchain") or {}).get("gcc") or {}
    if gcc.get("switch_to"):
        body.append("Includes a new compiler: GCC %s." % gcc["major"])
    if plan.get("news_unread"):
        body.append("%d unread news item%s." % (plan["news_unread"],
                                                "s" if plan["news_unread"] != 1 else ""))
    return title, " ".join(body)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Work out a verified update plan (read-only).")
    parser.add_argument("--json", help="write the plan as JSON to this file")
    parser.add_argument("--choices", help="JSON file of standing choices (see choices.json)")
    parser.add_argument("--request", help="plan an install request (JSON file: targets, "
                                          "requested settings, choices)")
    parser.add_argument("--live", action="store_true",
                        help="also check live packages for new upstream commits")
    parser.add_argument("--config-root", default="/",
                        help="analyze against another PORTAGE_CONFIGROOT")
    parser.add_argument("targets", nargs="*",
                        help="emerge targets/options (default: -uDN --with-bdeps=y @world)")
    args = parser.parse_args(argv)

    choices = None
    if args.choices:
        with open(args.choices) as f:
            choices = json.load(f)
    if args.request:
        # an install, planned as the user for review
        with open(args.request) as f:
            request = json.load(f)
        plan = Analyzer(request["targets"], args.config_root,
                        choices=request.get("choices"),
                        requested=request.get("requested") or ()).analyze()
        plan["request"] = request
        plan["signature"] = plan_signature(plan)
    else:
        plan = Analyzer(args.targets or None, args.config_root, choices=choices,
                        live=args.live).analyze()
    if args.json:
        with open(args.json, "w") as f:
            json.dump(plan, f, indent=1)
    print(format_report(plan))
    return {"up-to-date": 0, "ready": 0, "needs-approval": 0}.get(plan["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
