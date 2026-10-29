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
# give up after breaking this many cycles in a row
MAX_CYCLES = 8
# suggestions to try on one cycle before giving up on it
MAX_TRIES_PER_CYCLE = 6

CONFIG_DIRS = {
    "keywords": "package.accept_keywords",
    "use": "package.use",
    "unmask": "package.unmask",
    "licenses": "package.license",
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
        shutil.copytree(self.source, self.portage_dir, symlinks=True)
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


def _cp(cpv):
    return Atom("=" + cpv).cp


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


def _is_exact_pin(atom):
    """True for atoms that pin one version: =cat/pkg-1.0 or ~cat/pkg-1.0"""
    return atom.lstrip("!").startswith(("=", "~"))


class Analyzer:
    def __init__(self, targets=None, config_root="/", log=None):
        self.targets = targets or WORLD_ARGS
        self.config_root = config_root
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    # -- fixes -----------------------------------------------------------

    def build_fixes(self, needed, user_keywords):
        """Turn portage's needed changes into porthole-file lines"""
        fixes = []
        for pkg in needed.get("keywords", []):
            fixes.append({
                "kind": "keywords",
                "cpv": pkg["cpv"],
                "line": "=%s %s" % (pkg["cpv"], pkg["keyword"]),
                # a package pinned to a keyworded sibling should follow it
                "alt_line": "%s %s" % (pkg["cp"], pkg["keyword"]),
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
            if not lines:
                lines.append("Only a testing (%s) version satisfies the update."
                             % fix["line"].split()[-1])
        elif fix["kind"] == "use":
            for p in fix.get("parents", []):
                lines.append("%s requires %s." % (p["parent"], p["atom"]))
        elif fix["kind"] == "unmask":
            lines.append("The required version is masked by package.mask.")
        elif fix["kind"] == "licenses":
            lines.append("The required version uses a license you don't accept yet.")

        if world_pkgs:
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
            return run_worker(VERIFY_ARGS + (args or self.targets), scratch.root)

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
                if result.get("success") or (new and not _same_cycle(new["cycle"], seen)):
                    self.log("    breaks it" + ("" if result.get("success")
                                                 else ", next cycle found"))
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
                "title": "Update",
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

    def options(self):
        """emerge options from the targets, for follow-up commands"""
        return [a for a in self.targets if a.startswith("-") and a != "--oneshot"]

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
        if gcc.get("switch_to"):
            stages.append({
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

    # -- main entry -----------------------------------------------------

    def analyze(self):
        started = time.monotonic()
        user_keywords = read_config_atoms("keywords", self.config_root)

        self.log("Resolving (%s)..." % " ".join(self.targets))
        result = run_worker(SOLVE_ARGS + self.targets, self.config_root)
        if "error" in result and "merge_list" not in result:
            return {"status": "error", "error": result["error"],
                    "traceback": result.get("traceback")}
        self.log("  %d packages, %.0fs" % (len(result.get("merge_list") or []),
                                           result.get("seconds", 0)))

        fixes = self.build_fixes(result.get("needed_changes") or {}, user_keywords)
        for fix in fixes:
            self.explain(fix, result, user_keywords)

        plan = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "targets": self.targets,
            "merge_list": result.get("merge_list") or [],
            "fixes": fixes,
            "news_unread": news_unread(),
            "resolver_errors": result.get("errors") or {},
        }

        # prove it: same targets, our changes only, no autounmask
        self.log("Verifying %d proposed change(s)..." % len(fixes))
        check = self.verify(fixes)
        temp_use, broken = [], []
        if not check.get("success") and check.get("circular"):
            temp_use, broken, check = self.break_cycles(fixes, check)
        verified = bool(check.get("success"))

        for fix in fixes:
            fix["verified"] = verified
        plan["verified"] = verified
        plan["cycles_broken"] = broken
        stages = self.stages(temp_use, broken, verified)
        tc = self.toolchain(plan["merge_list"], result.get("system") or {})
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
            plan["notes"], plan["full_rebuild"] = self.toolchain_notes(
                tc, result.get("system") or {})
        if temp_use:
            # the temporary USE changes are in place from the first stage on
            stages[0]["config"] = {"use": temp_use, "file": TEMP_NAME}
        plan["stages"] = stages
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

        if not plan["merge_list"]:
            plan["status"] = "up-to-date"
        elif not verified:
            plan["status"] = "needs-help"
        elif fixes or temp_use or any(st.get("approval") for st in stages):
            plan["status"] = "needs-approval"
        else:
            plan["status"] = "ready"
        plan["seconds"] = round(time.monotonic() - started, 1)
        return plan


# -- text report -----------------------------------------------------------

KIND_NAMES = {"update": "U", "new": "N", "new-slot": "NS", "rebuild": "R", "downgrade": "D"}


def format_report(plan):
    out = []
    status = plan.get("status")
    if status == "error":
        return "Resolver failed: %s\n%s" % (plan["error"], plan.get("traceback") or "")
    merges = plan["merge_list"]
    counts = {}
    for m in merges:
        counts[m["kind"]] = counts.get(m["kind"], 0) + 1
    out.append({
        "up-to-date": "System is up to date.",
        "ready": "%d updates ready to install, no changes needed." % len(merges),
        "needs-approval": "%d updates; %d change(s) need your approval (verified to work)."
                          % (len(merges), len(plan["fixes"]) + len(plan.get("cycles_broken") or [])
                             + sum(1 for st in plan["stages"] if st.get("approval"))),
        "needs-help": "%d updates; could not find a verified solution automatically." % len(merges),
    }[status])
    if counts:
        out.append("  " + ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())))
    if plan.get("news_unread"):
        out.append("  %d unread news item(s): run 'eselect news read'" % plan["news_unread"])

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

    if status == "needs-help":
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
            out.append("  %s blocks %s" % (b["parent"], b["atom"]))
        for u in rem.get("unsatisfied") or []:
            out.append("  nothing satisfies %s (needed by %s)" % (u["atom"], u["parent"]))
        if plan.get("verify_error"):
            out.append("  resolver error: " + plan["verify_error"])

    out.append("\nUpdates:")
    for m in merges:
        old = " [%s]" % ", ".join(m["replaces"]) if m["replaces"] else ""
        out.append("  [%-2s] %s%s" % (KIND_NAMES.get(m["kind"], "?"), m["cpv"], old))
    out.append("\n(analysis took %.0fs)" % plan["seconds"])
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Work out a verified update plan (read-only).")
    parser.add_argument("--json", help="write the plan as JSON to this file")
    parser.add_argument("--config-root", default="/",
                        help="analyze against another PORTAGE_CONFIGROOT")
    parser.add_argument("targets", nargs="*",
                        help="emerge targets/options (default: -uDN --with-bdeps=y @world)")
    args = parser.parse_args(argv)

    plan = Analyzer(args.targets or None, args.config_root).analyze()
    if args.json:
        with open(args.json, "w") as f:
            json.dump(plan, f, indent=1)
    print(format_report(plan))
    return {"up-to-date": 0, "ready": 0, "needs-approval": 0}.get(plan["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
