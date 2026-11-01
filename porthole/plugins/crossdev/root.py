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
    return bd.environment(target, chost)


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
        # cross-built like everything else: the root's own linker, and distcc
        argv = ["emerge", "--verbose", "--color=y", "--oneshot", "--noreplace"] + missing
        log("== emulator: first %s, cross-built, so the system can build in itself"
            % " and ".join(missing))
        log("$ " + " ".join(argv))
        if not runner.run_logged(job_id, step, log, argv, extra_env=env):
            log("!! %s didn't build; can't build in the emulator without it"
                % " ".join(missing))
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
        if tools:
            argv = ["emerge", "--verbose", "--color=y", "--oneshot", "--keep-going",
                    "--buildpkg", "--usepkg", "--noreplace"] + tools
            # the tools may need changes the first plan didn't know of
            # (another Python target here): those the user allowed
            bd.apply_allowed(target, argv, env, log)
            log("== emulator: %d build tool%s needed in the system: cross-building them "
                "here (binary packages kept)" % (len(tools), "" if len(tools) == 1 else "s"))
            tried = set()
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
                                                "--keep-going", "--usepkg", "--noreplace"] + failed,
                                               inside)
                    log("$ " + " ".join(inner[inner.index("emerge"):]))
                    if not runner.run_logged(job_id, step, log, inner):
                        log("== emulator: not all of them built in the system; the emulator "
                            "builds the rest with the packages")
                        break
        with emulate.Chroot(target, log) as chroot:
            argv = emulate.chroot_argv(root, ["ldconfig"])
            runner.run_logged(job_id, step, log, argv)
            argv = chroot.emerge_argv(["--verbose", "--color=y", "--oneshot", "--keep-going",
                                       "--usepkg", "--noreplace"] + atoms, inside)
            log("== emulator: building %s in the system itself%s" % (
                " ".join(atoms), ", compiling here over distcc" if "distcc" in
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


def _run_stage(runner, job_id, step, log, target, stage, env, argvs):
    from porthole.plugins.crossdev import build as bd, emulate
    emulated = set()
    for argv in argvs:
        for attempt in range(6):
            offset = _log_size(job_id)
            log("$ " + " ".join(argv))
            if runner.run_logged(job_id, step, log, argv, extra_env=env):
                break
            failed = emulate.failed_packages(_log_since(job_id, offset), target.root)
            fresh = [a for a in failed if a not in emulated]
            if (runner.update(job_id) or {}).get("cancel"):
                return False
            if stage == "base" or not fresh:
                log("!! Some packages didn't build (above)%s. Starting the build again "
                    "picks up where it stopped." % (
                        ", also in the emulator" if failed and not fresh else ""))
                return False
            log("== %d package%s didn't cross-compile: %s; building %s in the emulator, "
                "then this stage again for the rest" % (
                    len(fresh), "" if len(fresh) == 1 else "s", " ".join(fresh),
                    "it" if len(fresh) == 1 else "them"))
            emulated.update(fresh)
            if not _emulate(runner, job_id, step, log, target, env, fresh):
                if not (runner.update(job_id) or {}).get("cancel"):
                    log("!! They didn't build in the emulator either (above).")
                return False
            env = _build_env(target, log)
            bd.apply_allowed(target, argv, env, log)
        else:
            log("!! Still failing after several rounds; stopping here.")
            return False
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


COMMANDS = {"check": check, "make-repo": make_repo, "new-root": new_root,
            "remove-root": remove_root, "toolchain": toolchain, "binpkgs": binpkgs,
            "settings": settings, "write-file": write_file, "profile": profile,
            "firstboot": firstboot, "packages": packages, "build": build}
STEPS = {"remove-root": step_remove_root, "toolchain": step_toolchain,
         "firstboot": step_firstboot, "build-check": step_build_check,
         "build-stage": step_build_stage}
