"""
    Porthole crossdev plugin (root half)

    Commands run through porthole-update-service as root (only while the
    plugin is switched on and installed where only root can change it):

        plugin crossdev check [ARCH [TUPLE]]   the readiness checks, as JSON
        plugin crossdev make-repo              create the local repository
                                               crossdev writes its toolchain
                                               ebuilds into
        plugin crossdev new-root NAME --profile REPO:PROFILE [--title T]
                                 [--repo R ...] [--keep-binpkgs]
                                               a new system for another
                                               computer (roots.py)
        plugin crossdev remove-root NAME       queue a job deleting one
        plugin crossdev reset-root NAME        queue starting one over: an empty
                                               root, its settings and binary
                                               packages kept
        plugin crossdev binpkgs NAME yes|no    keep binary packages of what is
                                               built for it, or just the system
        plugin crossdev toolchain NAME         queue a job building the cross
                                               toolchain its profile allows
        plugin crossdev settings NAME JSON     Porthole's settings block in its
                                               make.conf ({"values": {VAR: value},
                                               "machine": {"threads", "memory_gb"}},
                                               conf.py)
        plugin crossdev write-file NAME FILE JSON
                                               save one of its Portage files
                                               (make.conf, package.*, env/*;
                                               JSON: the text as a string)
        plugin crossdev profile NAME REPO:PROFILE
                                               point it at another profile
        plugin crossdev packages NAME JSON     what it installs: its world file and
                                               USE for single packages
                                               ({"world": [...], "use": {...}})
        plugin crossdev build NAME JSON        queue building it, with the changes
                                               the user agreed to ({"host_use",
                                               "root_use", "root_keywords"}: lines)
        plugin crossdev firstboot NAME FILE    its first-boot setup (FILE: JSON
                                               the front end wrote, mode 0600,
                                               password hashes, never passwords)

    Job steps (kind "plugin", plugin "crossdev"):

        remove-root   {"name": NAME}
        reset-root    {"name": NAME}
        toolchain     {"name": NAME}
        build-check   {"name": NAME}   compiler wrappers; emerge --pretend
        build-stage   {"name": NAME, "stage": base|system|extra|world}
        firstboot     {"name": NAME}   write its first-boot setup into it

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import os
import re
import shlex
import subprocess
import sys

from porthole.plugins.crossdev import checks

_ARCH = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_TUPLE = re.compile(r"^[a-z0-9_]+(-[a-z0-9_.]+){1,4}$")


def check(args):
    arch = args[0] if args else None
    tuple_ = args[1] if len(args) > 1 else None
    if (arch and not _ARCH.match(arch)) or (tuple_ and not _TUPLE.match(tuple_)) or len(args) > 2:
        print("usage: plugin crossdev check [ARCH [TUPLE]]", file=sys.stderr)
        return 2
    print(json.dumps([vars(c) for c in checks.check_all(arch, tuple_)], indent=1))
    return 0


def create_repo(portage_dir="/etc/portage", location=checks.REPO_DIR):
    """The repository crossdev needs, the way the Gentoo wiki sets it up:
    local, never synced, using the gentoo repository's eclasses. Returns
    what was done; ValueError if it can't be made."""
    from porthole.updater import system
    name = checks.REPO_NAME
    if name in system.repos_conf_sections(portage_dir):
        raise ValueError("There already is a repository called %s" % name)
    directory = os.path.join(portage_dir, "repos.conf")
    if os.path.isfile(directory):
        raise ValueError("%s is a single file. Porthole adds each repository in a file of "
                         "its own: move it into a new directory of that name first."
                         % directory)
    repo_name = os.path.join(location, "profiles", "repo_name")
    if os.path.exists(location):
        # left from before: used again only if it is this repository
        try:
            with open(repo_name) as f:
                mine = f.read().strip() == name
        except OSError:
            mine = False
        if not mine:
            raise ValueError("%s is there already and isn't a %s repository; move it "
                             "away first" % (location, name))
    else:
        os.makedirs(os.path.join(location, "profiles"), mode=0o755)
        os.makedirs(os.path.join(location, "metadata"), mode=0o755)
        with open(repo_name, "w") as f:
            f.write(name + "\n")
        with open(os.path.join(location, "metadata", "layout.conf"), "w") as f:
            f.write("masters = gentoo\nthin-manifests = true\n")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name + ".conf")
    with open(path + ".new", "w") as f:
        f.write("# Added in Porthole (crossdev plugin): crossdev's toolchain packages\n"
                "[%s]\nlocation = %s\npriority = 10\nmasters = gentoo\nauto-sync = no\n"
                % (name, location))
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    return "Created crossdev's repository %s (%s)" % (name, location)


def make_repo(args, portage_dir="/etc/portage", location=checks.REPO_DIR):
    if args:
        print("usage: plugin crossdev make-repo", file=sys.stderr)
        return 2
    try:
        print(create_repo(portage_dir, location))
    except (ValueError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def new_root(args):
    import argparse
    from porthole.plugins.crossdev import roots
    parser = argparse.ArgumentParser(prog="plugin crossdev new-root")
    parser.add_argument("name")
    parser.add_argument("--profile", required=True, help="REPO:PROFILE")
    parser.add_argument("--title", default="")
    parser.add_argument("--repo", action="append", default=[],
                        help="another of this computer's repositories to use")
    parser.add_argument("--keep-binpkgs", action="store_true",
                        help="keep binary packages of what is built for it")
    try:
        opts = parser.parse_args(args)
    except SystemExit as e:
        return e.code or 2
    repo, _, profile = opts.profile.partition(":")
    try:
        target = roots.create(opts.name, opts.title, repo, profile, opts.repo,
                              keep_binpkgs=opts.keep_binpkgs)
    except (roots.RootError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    print("Created %s in %s" % (target.name, target.root))
    return 0


def binpkgs(args):
    """plugin crossdev binpkgs NAME yes|no"""
    from porthole.plugins.crossdev import roots
    if len(args) != 2 or args[1] not in ("yes", "no"):
        print("usage: plugin crossdev binpkgs NAME yes|no", file=sys.stderr)
        return 2
    try:
        roots.set_binpkgs(args[0], args[1] == "yes")
    except (roots.RootError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    print("%s %s binary packages" % (args[0], "keeps" if args[1] == "yes" else "doesn't keep"))
    return 0


def health(args):
    """plugin crossdev health NAME: check a system as the last build step
    does, and show the findings (exit 1 when there are errors)"""
    from porthole.plugins.crossdev import health as hc, roots
    if len(args) != 1:
        print("usage: plugin crossdev health NAME", file=sys.stderr)
        return 2
    target = roots.made_here().get(args[0])
    if target is None:
        print("No system called %s was made here" % args[0], file=sys.stderr)
        return 1
    findings = hc.check(target, print)
    for line in hc.report_lines(findings) or ["No problems found."]:
        print(line)
    try:
        print("Report: " + hc.save_report(target, findings))
    except OSError as e:
        print("The report couldn't be saved: %s" % e, file=sys.stderr)
    return 1 if any(f.severity == hc.ERROR for f in findings) else 0


def remove_root(args):
    from porthole.plugins.crossdev import roots
    if len(args) != 1:
        print("usage: plugin crossdev remove-root NAME", file=sys.stderr)
        return 2
    try:
        target = roots.check_removable(args[0])
    except roots.RootError as e:
        print(e, file=sys.stderr)
        return 1
    return _queue("Delete the system %s" % target.title, "remove-root", target.name)


def _queue(title, action, name):
    from porthole.updater import runner
    job = runner.new_job("plugin", title, [{"kind": "plugin", "title": title,
                                            "plugin": "crossdev", "action": action,
                                            "name": name}], recheck=False)
    with runner.jobs_state() as state:
        state["jobs"].append(job)
        runner.prune(state)
    runner.start_runner()
    print(job["id"])
    return 0


def toolchain(args):
    from porthole.plugins.crossdev import roots
    if len(args) != 1:
        print("usage: plugin crossdev toolchain NAME", file=sys.stderr)
        return 2
    target = roots.made_here().get(args[0])
    if target is None:
        print("No system called %s was made here" % args[0], file=sys.stderr)
        return 1
    return _queue("Build the cross toolchain for %s" % target.title, "toolchain",
                  target.name)


def step_toolchain(runner, job_id, step, log):
    """Work the toolchain out again (as root, now), build it with
    crossdev, and let distccd run the new compiler"""
    import shutil
    from porthole.plugins.crossdev import roots, toolchain as tc
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    try:
        resolved = tc.resolve(target)
    except tc.ToolchainError as e:
        log("!! %s" % e)
        return False
    if not tc.needs_cross(resolved):
        log("%s is for %s, this computer's own: its own compiler builds for it, no "
            "cross toolchain needed." % (target.title, resolved["tuple"]))
        return True
    if not shutil.which("crossdev"):
        log("!! crossdev isn't installed (Ready to build for another computer?)")
        return False
    output = checks.repos().get(checks.REPO_NAME)
    if not output:
        # a local folder crossdev writes its toolchain ebuilds into: part of
        # building the toolchain, made here rather than sending the user off
        try:
            log(create_repo())
        except (ValueError, OSError) as e:
            log("!! crossdev's repository couldn't be made: %s" % e)
            return False
        output = checks.repos().get(checks.REPO_NAME)
    log("The toolchain %s's profile allows, for %s:" % (target.title, resolved["tuple"]))
    for title, version, repo, built in tc.compare(resolved):
        log("   %-15s %-22s from %s%s" % (title, version, repo,
                                          "  (built: %s)" % built if built else ""))
    argv = tc.command(resolved, output)
    # crossdev's settings first, then Porthole's unmask for the pinned
    # versions (crossdev's own "live" mask hides 9999.N ones), then a
    # check that all of them can be emerged, before hours of building
    log("$ " + " ".join(argv + ["--init-target"]))
    if not runner.run_logged(job_id, step, log, argv + ["--init-target"]):
        return False
    try:
        path = tc.write_unmask(resolved)
    except (tc.ToolchainError, OSError) as e:
        log("!! %s" % e)
        return False
    log("Unmasked exactly these versions in %s:" % path)
    for line in tc.unmask_lines(resolved):
        log("   " + line)
    check = tc.pretend_command(resolved)
    log("$ " + " ".join(check))
    if not runner.run_logged(job_id, step, log, check):
        log("!! The toolchain packages can't be emerged as they are (above); nothing "
            "was built.")
        return False
    log("$ " + " ".join(argv))
    if not runner.run_logged(job_id, step, log, argv):
        return False
    if shutil.which("distcc-config"):
        # distccd runs only the compilers listed in its masquerade directory
        log("$ distcc-config --update-masquerade-with-crossdev")
        if not runner.run_logged(job_id, step, log,
                                 ["distcc-config", "--update-masquerade-with-crossdev"]):
            return False
    return True


def reset_root(args):
    """plugin crossdev reset-root NAME: queue starting a system over"""
    from porthole.plugins.crossdev import roots
    if len(args) != 1:
        print("usage: plugin crossdev reset-root NAME", file=sys.stderr)
        return 2
    try:
        target = roots.check_removable(args[0])
    except roots.RootError as e:
        print(e, file=sys.stderr)
        return 1
    return _queue("Start %s over" % target.title, "reset-root", target.name)


def step_reset_root(runner, job_id, step, log):
    from porthole.plugins.crossdev import roots
    try:
        return roots.reset(step.get("name", ""), log=log)
    except (roots.RootError, OSError) as e:
        log("!! %s" % e)
        return False


def step_remove_root(runner, job_id, step, log):
    from porthole.plugins.crossdev import roots
    try:
        return roots.remove(step.get("name", ""), log=log)
    except (roots.RootError, OSError) as e:
        log("!! %s" % e)
        return False


def _system(name):
    from porthole.plugins.crossdev import roots
    target = roots.made_here().get(name)
    if target is None:
        print("No system called %s was made here" % name, file=sys.stderr)
    return target


def settings(args):
    """plugin crossdev settings NAME JSON"""
    from porthole.plugins.crossdev import conf
    if len(args) != 2:
        print("usage: plugin crossdev settings NAME JSON", file=sys.stderr)
        return 2
    target = _system(args[0])
    if target is None:
        return 1
    try:
        request = json.loads(args[1])
        values = request.get("values") if isinstance(request, dict) else None
        if not isinstance(values, dict) or not all(
                isinstance(k, str) and (v is None or isinstance(v, str))
                for k, v in values.items()):
            raise ValueError('expected {"values": {VAR: value}, "machine": {...}}')
        conf.write(target, values, request.get("machine"))
    except (ValueError, conf.ConfError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    print("Saved %s's settings" % target.title)
    return 0


def write_file(args):
    """plugin crossdev write-file NAME FILE JSON"""
    from porthole.plugins.crossdev import conf
    if len(args) != 3:
        print("usage: plugin crossdev write-file NAME FILE JSON", file=sys.stderr)
        return 2
    target = _system(args[0])
    if target is None:
        return 1
    try:
        text = json.loads(args[2])
        if not isinstance(text, str):
            raise ValueError("expected the text as a JSON string")
        print(conf.write_file(target, args[1], text))
    except (ValueError, conf.ConfError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def profile(args):
    """plugin crossdev profile NAME REPO:PROFILE"""
    from porthole.plugins.crossdev import roots
    if len(args) != 2 or ":" not in args[1]:
        print("usage: plugin crossdev profile NAME REPO:PROFILE", file=sys.stderr)
        return 2
    repo, _, name = args[1].partition(":")
    try:
        print(roots.set_profile(args[0], repo, name))
    except (roots.RootError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def firstboot(args):
    """plugin crossdev firstboot NAME FILE"""
    from porthole.plugins.crossdev import firstboot as fb
    if len(args) != 2:
        print("usage: plugin crossdev firstboot NAME FILE", file=sys.stderr)
        return 2
    target = _system(args[0])
    if target is None:
        return 1
    try:
        if os.path.getsize(args[1]) > 64 * 1024:
            raise ValueError("That file is too big")
        with open(args[1]) as f:
            config = json.load(f)
        config = fb.save(target.name, config)
        fb.write_choice_files(target, config)
    except (OSError, ValueError, fb.FirstBootError) as e:
        print(e, file=sys.stderr)
        return 1
    print("Saved %s's first-boot setup" % target.title)
    return 0


def packages(args):
    """plugin crossdev packages NAME JSON"""
    from porthole.plugins.crossdev import packages as pk
    if len(args) != 2:
        print("usage: plugin crossdev packages NAME JSON", file=sys.stderr)
        return 2
    target = _system(args[0])
    if target is None:
        return 1
    try:
        request = pk.save(target, json.loads(args[1]))
    except (ValueError, pk.PackagesError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    count = len(request["world"])
    print("%s installs %d package%s" % (target.title, count, "" if count == 1 else "s"))
    return 0


def build(args):
    """plugin crossdev build NAME JSON"""
    from porthole.plugins.crossdev import build as bd
    from porthole.updater import runner
    if len(args) != 2:
        print("usage: plugin crossdev build NAME JSON", file=sys.stderr)
        return 2
    target = _system(args[0])
    if target is None:
        return 1
    try:
        changes = json.loads(args[1])
        if not isinstance(changes, dict):
            raise ValueError("expected an object")
        for path in bd.write_changes(target, changes.get("host_use"), changes.get("root_use"),
                                     changes.get("root_keywords")):
            print("Wrote %s" % path)
        bd.write_policy(target, bool(changes.get("allow_python")))
    except (ValueError, bd.BuildError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    title = "Build %s" % target.title

    def step(action, step_title, **more):
        return dict({"kind": "plugin", "title": step_title, "plugin": "crossdev",
                     "action": action, "name": target.name}, **more)
    steps = [step("build-check", "Check the build can go ahead")]
    steps += [step("build-stage", stage_title, stage=stage) for stage, stage_title in bd.STAGES]
    steps.append(step("firstboot", "The first-boot setup"))
    steps.append(step("health", "Check the finished system"))
    job = runner.new_job("plugin", title, steps, recheck=False)
    with runner.jobs_state() as state:
        state["jobs"].append(job)
        runner.prune(state)
    runner.start_runner()
    print(job["id"])
    return 0


def _build_env(target, log):
    from porthole.plugins.crossdev import build as bd
    chost = bd.chost_of(target)
    if not chost:
        raise bd.BuildError("%s's settings give no CHOST" % target.title)
    bd.write_wrappers(target, chost)
    if chost != bd.host_chost():
        bd.write_ldd(target, chost)
        bd.write_bashrc(target, chost)
        bd.link_python_modules(target, log)
        bd.repair_perl_modules(target, log)
    return bd.environment(target, chost)


# what each step does, for the job's command record (recipe.py)
RECIPE_NOTES = {
    "toolchain": (
        "crossdev builds a cross toolchain: binutils, gcc, the kernel headers and the\n"
        "C library for another CPU, installed beside this computer's own compiler\n"
        "(as <CHOST>-gcc). Its ebuilds go into a local repository of their own."),
    "reset-root": (
        "Starting over: the system's folder is emptied, its settings kept."),
    "build-check": (
        "Before anything is built, Porthole writes what a cross build needs and asks\n"
        "portage to work the whole build out (emerge --pretend). How portage is told\n"
        "to build for another computer, all in the exported settings below:\n"
        "  ROOT                where packages are installed (the system's folder)\n"
        "  SYSROOT             where the compiler finds the system's headers and\n"
        "                      libraries (the same folder)\n"
        "  PORTAGE_CONFIGROOT  whose /etc/portage applies: the system's, not this\n"
        "                      computer's (its profile, make.conf, package.use ...)\n"
        "  CHOST / CBUILD      the computer built for / the computer building\n"
        "  PATH                Porthole's compiler wrappers first (they add --sysroot)\n"
        "  QEMU_LD_PREFIX      the system's libraries, for its programs run by qemu"),
    ("build-stage", "base"): (
        "The kernel headers and the C library first: everything else is compiled\n"
        "against them. --usepkg installs a binary package already made with the same\n"
        "USE flags instead of compiling again."),
    ("build-stage", "system"): (
        "@system: the base set the profile lists (shell, coreutils, portage's own\n"
        "needs ...), cross-compiled into the system."),
    ("build-stage", "extra"): (
        "What the First boot choices need that the system's list doesn't have yet."),
    ("build-stage", "world"): (
        "@world: the system's own list of packages (its var/lib/portage/world) and\n"
        "everything they need."),
    "emulate": (
        "Some packages can't be cross-compiled: their build runs what it just built, or\n"
        "their build system doesn't support it. They are built inside the system itself:\n"
        "chroot into its folder (with /proc, /sys, /dev and the repositories mounted\n"
        "in), its arm64 programs run by qemu through binfmt_misc, while the compiling\n"
        "goes back to this computer's cross compiler over distcc."),
    "break-cycle": (
        "emerge stopped on a circular dependency (A needs B, B needs A) and suggested\n"
        "USE flags to turn off for a first build. That build uses a temporary\n"
        "package.use file; the stage then runs again, building it with its usual flags."),
    "health": (
        "The finished system checked: no programs built for this computer, every\n"
        "library a program needs present, perl and Python modules under the system's\n"
        "names (also in its binary packages), kernel modules indexed. Errors fail the\n"
        "build; warnings are only reported. The same check by hand:\n"
        "  porthole-update-service plugin crossdev health NAME"),
    "firstboot": (
        "The library cache (ldconfig, run in the system), then the First boot setup\n"
        "written into it: the device name, accounts and services chosen. Those files\n"
        "aren't copied here: they hold password hashes."),
}


def recipe_note(step):
    """(note, key it is given once under) for the job's command record"""
    action = step.get("action")
    key = (action, step.get("stage")) if action == "build-stage" else action
    note = RECIPE_NOTES.get(key)
    return (note, key) if note else (None, None)


def _record_settings(target, chost):
    """The system's settings as portage reads them, into the command record
    (once a job): what writing them by hand would take"""
    from porthole.updater import recipe
    from porthole.plugins.crossdev import build as bd, toolchain as tc
    record = recipe.current()
    if record is None or "settings" in record.explained:
        return
    record.explained.add("settings")
    record.heading("Settings portage reads for %s (PORTAGE_CONFIGROOT=%s), as they were "
                   "when the build started" % (target.title, target.config_root))
    record.note("make.profile picks the profile; make.conf the system-wide settings;\n"
                "package.* per package; repos.conf the repositories; bashrc runs in each\n"
                "package's build (Porthole's part fixes names a cross build gets wrong).")
    record.folder(target.portage_dir)
    wrappers = bd.wrappers_dir(target)
    if os.path.isdir(wrappers):
        record.heading("Porthole's compiler wrappers (first in PATH)")
        record.note("Each runs the cross compiler with --sysroot, so it uses the system's\n"
                    "headers and libraries, never this computer's.")
        record.folder(wrappers)
    if chost != bd.host_chost():
        record.heading("The cross toolchain (built earlier, by its own job)")
        installed = tc.installed(chost)
        if installed:
            record.note("Installed: " + ", ".join("%s %s" % kv for kv in sorted(installed.items())))
        try:
            resolved = tc.resolve(target)
            output = checks.repos().get(checks.REPO_NAME)
            if output:
                record.note("Built with:\n  " + shlex.join(tc.command(resolved, output)))
            unmask = os.path.join("/etc/portage/package.unmask",
                                  "porthole-cross-" + resolved["tuple"])
            if os.path.isfile(unmask):
                record.file_contents(unmask)
        except Exception:
            pass


def step_build_check(runner, job_id, step, log):
    import shutil
    from porthole.plugins.crossdev import build as bd, toolchain as tc
    from porthole.plugins.crossdev import roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    try:
        env = _build_env(target, log)
    except (bd.BuildError, OSError) as e:
        log("!! %s" % e)
        return False
    chost = env["CHOST"]
    try:
        _record_settings(target, chost)
    except Exception as e:  # the record is a help, never a reason to stop
        log("!! couldn't note the settings in the command record: %s" % e)
    if env["CBUILD"] != chost:
        if not shutil.which(chost + "-gcc"):
            log("!! There is no cross compiler for %s yet: build its toolchain first "
                "(Systems window)" % chost)
            return False
        built = tc.installed(chost)
        log("Cross toolchain for %s: %s" % (chost, ", ".join(
            "%s %s" % kv for kv in sorted(built.items())) or "?"))
        # some builds run the programs they just made (meson's exe_wrapper,
        # gobject-introspection): Gentoo's eclasses run them with qemu-<arch>
        qemu = bd.qemu_name(chost)
        if not shutil.which(qemu):
            log("!! Warning: %s isn't installed (app-emulation/qemu with qemu_user_targets "
                "for it). Packages that run what they build while building (glib, "
                "gobject-introspection and what uses it) will fail; \"Ready to build for "
                "another computer?\" adds it to the install queue." % qemu)
        else:
            log("Programs built for %s run here with %s when a build needs it"
                % (chost, qemu))
    log("Building into %s, as its own sysroot; compilers from %s"
        % (env["ROOT"], bd.wrappers_dir(target)))
    argv = bd.check_argv(target)
    log("$ " + " ".join(argv))
    if not runner.run_logged(job_id, step, log, argv, extra_env=env):
        log("!! Portage can't work the build out as it stands (above); nothing was built.")
        return False
    return True


def _log_size(job_id):
    from porthole.updater import runner as runner_module
    try:
        return os.path.getsize(os.path.join(runner_module.JOB_LOG_DIR, job_id + ".log"))
    except OSError:
        return 0


def _log_since(job_id, offset):
    from porthole.updater import runner as runner_module
    try:
        with open(os.path.join(runner_module.JOB_LOG_DIR, job_id + ".log"),
                  errors="replace") as f:
            f.seek(offset)
            return f.read()
    except OSError:
        return ""


def _emulate(runner, job_id, step, log, target, env, atoms):
    """Build atoms inside the system under qemu, compiling over distcc on
    this computer; True when they all built"""
    from porthole.plugins.crossdev import build as bd, emulate
    from porthole.updater.ondemand import Helpers, HelperError
    root = os.path.realpath(target.root)
    missing = [cp for cp in emulate.NEEDED if not emulate.installed(root, cp)]
    if missing:
        # cross-built like everything else: the root's own linker, distcc,
        # and portage (emerge must exist in the chroot)
        argv = ["emerge", "--verbose", "--color=y", "--oneshot", "--noreplace", "--usepkg",
                "--keep-going"] + missing
        log("== emulator: first %s, cross-built, so the system can build in itself"
            % " and ".join(missing))
        log("$ " + " ".join(argv))
        runner.run_logged(job_id, step, log, argv, extra_env=env)
        still = [cp for cp in emulate.NEEDED if not emulate.installed(root, cp)]
        if still:
            log("!! %s didn't build; can't build in the emulator without %s"
                % (" and ".join(still), "it" if len(still) == 1 else "them"))
            return False
    helpers = Helpers(job_id, ["binfmt", "distcc"])
    try:
        helper_env = helpers.start(log)
    except HelperError as e:
        log("!! %s" % e)
        return False
    try:
        # distcc's compiler links in the system (a cross-installed distcc has
        # none), so the emulated builds really hand their compiling here
        emulate.distcc_links(root, log)
        inside = emulate.emulated_env(target, helper_env)
        # what building them in the system would bring in: mostly build
        # tools (in a cross build those run on this computer). Those are
        # cross-built here instead of compiled under qemu, with binary
        # packages kept, outside the chroot: while it is set up, the root
        # has this computer's /dev, /run and /sys mounted in it
        with emulate.Chroot(target, log) as chroot:
            argv = emulate.chroot_argv(root, ["ldconfig"])
            log("$ " + " ".join(argv))
            if not runner.run_logged(job_id, step, log, argv):
                log("!! The system's ldconfig didn't run")
                return False
            pretend = chroot.emerge_argv(["--pretend", "--oneshot", "--color=n"] + atoms, inside)
            log("== emulator: working out what building them in the system needs")
            result = subprocess.run(pretend, capture_output=True, text=True)
            from porthole.updater import recipe
            recipe.ran(pretend, result.returncode)
        wanted = {emulate.cp_of(a) for a in atoms}
        # what they need to be built: what emerge merges before the first of
        # them. What comes after needs them (shared-mime-info after glib): a
        # cross build of it would pull them in again, so it is left for the
        # system, after them
        tools = []
        for atom in emulate.merge_list(result.stdout):
            if emulate.cp_of(atom) in wanted:
                break
            tools.append(atom)
        tried = set()  # failed tools the loop below builds in the system
        if tools:
            argv = ["emerge", "--verbose", "--color=y", "--oneshot", "--keep-going",
                    "--buildpkg", "--usepkg", "--noreplace"] + tools
            # the tools may need changes the first plan didn't know of
            # (another Python target here): those the user allowed
            bd.apply_allowed(target, argv, env, log)
            log("== emulator: %d build tool%s needed in the system: cross-building them "
                "here (binary packages kept)" % (len(tools), "" if len(tools) == 1 else "s"))
            for _round in range(4):
                offset = _log_size(job_id)
                log("$ " + " ".join(argv))
                if runner.run_logged(job_id, step, log, argv, extra_env=env):
                    break
                # the tools that really failed (perl XS modules, say) are
                # built in the system; those dropped for them cross-compile
                # once they are in
                failed = [a for a in emulate.failed_packages(_log_since(job_id, offset), root)
                          if a not in tried]
                if not failed:
                    log("== emulator: not all of them cross-compiled; the emulator builds "
                        "the rest")
                    break
                tried.update(failed)
                log("== emulator: %s didn't cross-compile; building %s in the system, then "
                    "the other tools here again" % (" ".join(failed),
                                                    "it" if len(failed) == 1 else "them"))
                with emulate.Chroot(target, log) as chroot:
                    runner.run_logged(job_id, step, log, emulate.chroot_argv(root, ["ldconfig"]))
                    inner = chroot.emerge_argv(["--verbose", "--color=y", "--oneshot",
                                                "--keep-going", "--usepkg"] + failed, inside)
                    log("$ " + " ".join(inner[inner.index("emerge"):]))
                    if not runner.run_logged(job_id, step, log, inner):
                        log("== emulator: not all of them built in the system; the emulator "
                            "builds the rest with the packages")
                        break
        with emulate.Chroot(target, log) as chroot:
            argv = emulate.chroot_argv(root, ["ldconfig"])
            runner.run_logged(job_id, step, log, argv)
            # what failed to cross-compile, built again here even when that
            # version is installed: emerge wanted it rebuilt (changed USE, a
            # subslot), and --noreplace left nothing to do (seen: pulseaudio-
            # daemon going back and forth). Not what the tool loop just built.
            left = [a for a in atoms if a not in tried]
            if not left:
                return True
            argv = chroot.emerge_argv(["--verbose", "--color=y", "--oneshot", "--keep-going",
                                       "--usepkg"] + left, inside)
            log("== emulator: building %s in the system itself%s" % (
                " ".join(left), ", compiling here over distcc" if "distcc" in
                inside["FEATURES"].split() else ""))
            log("$ " + " ".join(argv[argv.index("emerge"):]))
            return runner.run_logged(job_id, step, log, argv)
    except (emulate.EmulateError, OSError) as e:
        log("!! The emulator couldn't be set up: %s" % e)
        return False
    finally:
        helpers.stop(log)


def step_build_stage(runner, job_id, step, log):
    """One stage of the build, cross-compiled; packages that fail are built
    in the system under qemu (compiling over distcc), then the stage runs
    again to finish the rest of its dependency graph"""
    from porthole.plugins.crossdev import build as bd, emulate, roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    stage = step.get("stage", "")
    try:
        env = _build_env(target, log)
        argvs = bd.commands(target, stage)
    except (bd.BuildError, OSError) as e:
        log("!! %s" % e)
        return False
    if not argvs:
        log("Nothing to do here.")
        return True
    # binfmt on for the whole stage when the system is for another CPU, so
    # the system's programs a cross build runs work (with QEMU_LD_PREFIX)
    from porthole.updater.ondemand import Helpers, HelperError
    helpers = Helpers(job_id, ["binfmt"] if env["CHOST"] != env["CBUILD"] else [])
    try:
        helpers.start(log)
    except HelperError as e:
        log("!! %s (programs built for the system can't run during the cross build)" % e)
        helpers = Helpers(job_id, [])
    try:
        return _run_stage(runner, job_id, step, log, target, stage, env, argvs)
    finally:
        helpers.stop(log)


CYCLE_FILE = "zz-porthole-cycle"


def _fix_step(step, action, title, **more):
    """A step that fixes what a stage ran into, for the same system"""
    return dict({"kind": "plugin", "title": title, "plugin": "crossdev", "action": action,
                 "name": step.get("name", "")}, **more)


def step_break_cycle(runner, job_id, step, log):
    """emerge stopped on a circular dependency and suggested USE changes
    (pam -elogind while elogind needs pam): build the first package that
    builds that way with it, from a temporary package.use file of the
    system's, then take the file away; the stage, run again next, rebuilds
    it with its own flags"""
    from porthole.plugins.crossdev import roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    from porthole.plugins.crossdev import build as bd
    try:
        env = _build_env(target, log)
    except (bd.BuildError, OSError) as e:
        log("!! %s" % e)
        return False
    path = os.path.join(target.portage_dir, "package.use", CYCLE_FILE)
    for cpv, flags in step.get("suggestions") or []:
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        with open(path, "w") as f:
            f.write("# Porthole: for breaking a circular dependency, only while it builds\n"
                    "=%s %s\n" % (cpv, flags))
        from porthole.updater import recipe
        if recipe.current() is not None:
            recipe.current().file_contents(path)
        log("== building %s with USE \"%s\" (emerge's suggestion); the stage after this "
            "builds it again with its usual flags" % (cpv, flags))
        argv = ["emerge", "--verbose", "--color=y", "--oneshot", "=" + cpv]
        log("$ " + " ".join(argv))
        try:
            ok = runner.run_logged(job_id, step, log, argv, extra_env=env)
        finally:
            os.unlink(path)
            recipe.ran(["rm", path])
        if ok:
            return True
        if (runner.update(job_id) or {}).get("cancel"):
            return False
        log("!! %s didn't build that way either" % cpv)
    return False


def step_emulate(runner, job_id, step, log):
    """Build packages that didn't cross-compile inside the system, under
    qemu (compiling over distcc on this computer)"""
    from porthole.plugins.crossdev import roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    from porthole.plugins.crossdev import build as bd
    try:
        env = _build_env(target, log)
    except (bd.BuildError, OSError) as e:
        log("!! %s" % e)
        return False
    if _emulate(runner, job_id, step, log, target, env, list(step.get("atoms") or [])):
        return True
    if not (runner.update(job_id) or {}).get("cancel"):
        log("!! They didn't build in the emulator either (above).")
    return False


def _run_stage(runner, job_id, step, log, target, stage, env, argvs):
    """The stage's emerge commands in turn. When one fails in a way the
    build can fix, the fix goes into the job as the next step (circular
    dependency broken; packages built in the emulator), then this stage
    again from that command: AGAIN. What earlier rounds already fixed comes
    with the step, so the same fix isn't tried twice."""
    from porthole.plugins.crossdev import build as bd, emulate
    from porthole.updater.runner import AGAIN
    emulated, cycles = set(step.get("emulated") or ()), set(step.get("cycles") or ())
    first = min(int(step.get("start_at") or 0), len(argvs) - 1)
    if step.get("attempt", 1) > 1:
        # what the fix built may let the build make changes it can make itself
        bd.apply_allowed(target, argvs[first], env, log)
    for index in range(first, len(argvs)):
        argv = argvs[index]
        offset = _log_size(job_id)
        log("$ " + " ".join(argv))
        if runner.run_logged(job_id, step, log, argv, extra_env=env):
            continue
        if (runner.update(job_id) or {}).get("cancel"):
            return False
        text = _log_since(job_id, offset)
        failed = emulate.failed_packages(text, target.root)
        fresh = [a for a in failed if a not in emulated]
        again = {"start_at": index}
        if not failed:
            suggestions = [[cpv, flags] for cpv, flags in emulate.cycle_suggestions(text)
                           if cpv not in cycles]
            if suggestions:
                log("== circular dependency: emerge suggests building %s with other USE "
                    "flags first. That is the next step, then this stage again."
                    % " or ".join(c for c, _f in suggestions))
                again.update(cycles=sorted(cycles | {c for c, _f in suggestions}),
                             emulated=sorted(emulated))
                fix = _fix_step(step, "break-cycle", "Break a circular dependency: %s"
                                % suggestions[0][0], suggestions=suggestions)
                if runner.add_steps(job_id, [fix], again, why="circular dependency"):
                    return AGAIN
                log("!! This stage has been run again too often; stopping here.")
                return False
        if stage == "base" or not fresh:
            log("!! Some packages didn't build (above)%s. Starting the build again "
                "picks up where it stopped." % (
                    ", also in the emulator" if failed and not fresh else ""))
            return False
        log("== %d package%s didn't cross-compile: %s. Building %s in the emulator is the "
            "next step, then this stage again for the rest." % (
                len(fresh), "" if len(fresh) == 1 else "s", " ".join(fresh),
                "it" if len(fresh) == 1 else "them"))
        again.update(emulated=sorted(emulated | set(fresh)), cycles=sorted(cycles))
        fix = _fix_step(step, "emulate", "Build in the emulator: %s" % " ".join(fresh),
                        atoms=fresh)
        if runner.add_steps(job_id, [fix], again, why="didn't cross-compile"):
            return AGAIN
        log("!! This stage has been run again too often; stopping here.")
        return False
    return True


def step_health(runner, job_id, step, log):
    """The last step of a build: is what was built really the system's?
    Errors fail the build (nothing should be packaged from it); warnings are
    only reported"""
    from porthole.plugins.crossdev import health as hc, roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    findings = hc.check(target, log)
    errors = [f for f in findings if f.severity == hc.ERROR]
    warnings = [f for f in findings if f.severity == hc.WARNING]
    for line in hc.report_lines(findings):
        log(("!! " if line.startswith("Error") else "") + line)
    try:
        log("The full report: " + hc.save_report(target, findings))
    except OSError as e:
        log("!! The report couldn't be saved: %s" % e)
    if errors:
        log("!! %s isn't healthy: %d error%s (above). Fix them, then check it again "
            "(porthole-update-service plugin crossdev health %s)." % (
                target.title, len(errors), "" if len(errors) == 1 else "s", target.name))
        return False
    log("%s is healthy%s." % (target.title, ", with %d warning%s to look at" % (
        len(warnings), "" if len(warnings) == 1 else "s") if warnings else ""))
    return True


def step_firstboot(runner, job_id, step, log):
    """Write the first-boot setup into the system (the end of its build)"""
    from porthole.plugins.crossdev import firstboot as fb, roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    # the library cache the system boots with (portage can't make it while
    # cross-building)
    from porthole.plugins.crossdev import emulate
    if not emulate.ldconfig(runner, job_id, step, log, target):
        return False
    config = fb.read(target.name)
    if not config:
        log("No first-boot setup for %s" % target.title)
        return True
    try:
        return fb.apply(target, config, log)
    except OSError as e:
        log("!! %s" % e)
        return False


COMMANDS = {"check": check, "health": health, "make-repo": make_repo, "new-root": new_root,
            "remove-root": remove_root, "reset-root": reset_root, "toolchain": toolchain,
            "binpkgs": binpkgs,
            "settings": settings, "write-file": write_file, "profile": profile,
            "firstboot": firstboot, "packages": packages, "build": build}
STEPS = {"remove-root": step_remove_root, "reset-root": step_reset_root,
         "toolchain": step_toolchain,
         "firstboot": step_firstboot, "build-check": step_build_check,
         "build-stage": step_build_stage, "emulate": step_emulate,
         "break-cycle": step_break_cycle, "health": step_health}
