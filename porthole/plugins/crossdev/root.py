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
        plugin crossdev export NAME FOLDER     copy it to FOLDER (empty, or an
                                               earlier copy) for its computer:
                                               repositories copied in, its
                                               profile and repos.conf there

    Job steps (kind "plugin", plugin "crossdev"):

        remove-root   {"name": NAME}
        reset-root    {"name": NAME}
        toolchain     {"name": NAME}
        build-check   {"name": NAME}   compiler wrappers; emerge --pretend
        build-stage   {"name": NAME, "stage": base|system|extra|world}
        kernel        {"name": NAME}   install kernels merged into it, inside it
        depclean      {"name": NAME}   remove what nothing in it needs any more
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


def shell(args):
    """plugin crossdev shell NAME: a root shell inside the system, to change
    things by hand (its programs run through qemu; emerge in it compiles
    over distcc here). Needs a terminal. Everything is taken away again when
    the shell ends."""
    import fcntl
    from porthole.plugins.crossdev import emulate, roots
    from porthole.updater import runner
    from porthole.updater.ondemand import Helpers, HelperError
    if len(args) != 1:
        print("usage: plugin crossdev shell NAME", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("The shell needs a terminal: run it in one.", file=sys.stderr)
        return 2
    target = roots.made_here().get(args[0])
    if target is None:
        print("No system called %s was made here" % args[0], file=sys.stderr)
        return 1
    with runner.jobs_state(write=False) as state:
        busy = [j for j in runner.active_jobs(state) if any(
            s.get("plugin") == "crossdev" and s.get("name") == target.name
            for s in j.get("steps") or [])]
    if busy:
        print("%s has a job %s (%s): open a shell once it has finished." % (
            target.title, busy[0]["status"], busy[0]["title"]), file=sys.stderr)
        return 1
    root = os.path.realpath(target.root)
    if not os.path.exists(os.path.join(root, "bin/sh")):
        print("%s has nothing to run yet: build it first." % target.title, file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(emulate.SHELL_LOCK), exist_ok=True)
    lock = open(emulate.SHELL_LOCK % target.name, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("A shell is already open in %s." % target.title, file=sys.stderr)
        return 1
    from porthole.plugins.crossdev import build as bd
    try:
        _build_env(target, print)
    except (bd.BuildError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    bd.distcc_names(bd.chost_of(target))
    helpers = Helpers("shell-" + target.name, ["binfmt", "distcc"],
                      distcc={"path": bd.wrappers_dir(target)})
    try:
        helper_env = helpers.start(print)
    except HelperError as e:
        print(e, file=sys.stderr)
        return 1
    try:
        emulate.distcc_links(root, print)
        inside = emulate.emulated_env(target, helper_env)
        with emulate.Chroot(target, print):
            print("\n%s, inside the system (%s). Its own programs run through qemu, so\n"
                  "they are slower than usual; emerge compiles over distcc on this computer.\n"
                  "Changes are kept. Type exit to leave: everything mounted for this is\n"
                  "taken away again.\n" % (target.title, root))
            shell_cmd = "/bin/bash" if os.path.exists(os.path.join(root, "bin/bash")) \
                else "/bin/sh"
            if os.environ.get("TERM"):
                inside["TERM"] = os.environ["TERM"]
            # Ctrl-C is the shell's (and what runs in it): not ours, or the
            # mounts would be taken away under a shell still open
            import signal
            keys = (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP)
            before = [signal.signal(k, signal.SIG_IGN) for k in keys]
            try:
                code = subprocess.call(
                    emulate.chroot_argv(root, [shell_cmd, "-l"], inside),
                    preexec_fn=lambda: [signal.signal(k, signal.SIG_DFL) for k in keys])
            finally:
                for k, old in zip(keys, before):
                    signal.signal(k, old)
        print("Left %s." % target.title)
        return code if code >= 0 else 128 - code  # ended by a signal, as a shell says
    except emulate.EmulateError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        emulate.drop_distcc_links(root)
        helpers.stop(print)
        lock.close()


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


def export(args):
    """plugin crossdev export NAME FOLDER: copy a built system to FOLDER (an
    SD card's root partition, say) with settings for the computer it runs
    on: its repositories copied in, make.profile and repos.conf pointing at
    them (export.py)"""
    from porthole.plugins.crossdev import export as ex, roots
    from porthole.updater import runner
    if len(args) != 2:
        print("usage: plugin crossdev export NAME FOLDER", file=sys.stderr)
        return 2
    target = roots.made_here().get(args[0])
    if target is None:
        print("No system called %s was made here" % args[0], file=sys.stderr)
        return 1
    with runner.jobs_state(write=False) as state:
        busy = [j for j in runner.active_jobs(state) if any(
            s.get("plugin") == "crossdev" and s.get("name") == target.name
            for s in j.get("steps") or [])]
    if busy or roots.mounts_under(target.root):
        print("%s is in use (%s): copy it once that has finished." % (
            target.title, busy[0]["title"] if busy else "things are mounted in it"),
            file=sys.stderr)
        return 1
    try:
        dest = ex.export(target, args[1], roots.used_repos(target))
    except (ex.ExportError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    print("%s copied to %s." % (target.title, dest))
    return 0


def images(args):
    """plugin crossdev images NAME: the images the system's packages say it
    can be made into (image.py), and what each still needs"""
    from porthole.plugins.crossdev import image as im
    if len(args) != 1:
        print("usage: plugin crossdev images NAME", file=sys.stderr)
        return 2
    target = _system(args[0])
    if target is None:
        return 1
    found, broken = im.recipes(target.root)
    for path, error in broken:
        print("!! %s" % error)
    if not found:
        print("%s's packages give no image recipes (%s)." % (
            target.title, "/" + im.RECIPES_DIR))
    for recipe in found.values():
        missing = im.missing_needs(target.root, recipe) + [
            "%s isn't in it" % s for s in im.missing_sources(target.root, recipe)]
        print("%s: %s%s" % (recipe.id, recipe.title, "" if missing else " (ready)"))
        for line in missing:
            print("    needs: %s" % line)
    return 0


def package(args):
    """plugin crossdev package NAME RECIPE: queue making the image RECIPE of
    the system (image.py)"""
    from porthole.plugins.crossdev import image as im
    if len(args) != 2:
        print("usage: plugin crossdev package NAME RECIPE", file=sys.stderr)
        return 2
    target = _system(args[0])
    if target is None:
        return 1
    found, _broken = im.recipes(target.root)
    recipe = found.get(args[1])
    if recipe is None:
        print("%s has no image recipe %s" % (target.title, args[1]), file=sys.stderr)
        return 1
    return _queue("Make %s of %s" % (recipe.title, target.title), "package", target.name,
                  recipe=recipe.id)


def step_package(runner, job_id, step, log):
    """Make an image of the system from one of its recipes"""
    from porthole.plugins.crossdev import emulate, image as im, roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    try:
        emulate.refuse_if_shell(target)
    except emulate.EmulateError as e:
        log("!! %s" % e)
        return False
    if roots.mounts_under(target.root):
        log("!! Things are mounted in %s: make the image once that has finished."
            % target.title)
        return False
    found = im.recipes(target.root)[0]
    if step.get("recipe") == "livecd":
        # the end of a live system's build: its live images, by whichever
        # recipes its packages installed during the build
        wanted = im.live_recipes(found)
        if not wanted:
            log("No package in %s gives a recipe for a live image (/%s): none made."
                % (target.title, im.RECIPES_DIR))
            return True
    else:
        wanted = [found.get(step.get("recipe", ""))]
        if wanted[0] is None:
            log("!! %s has no image recipe %s any more" % (target.title, step.get("recipe")))
            return False
    for recipe in wanted:
        try:
            card = im.make(target, recipe, roots.used_repos(target),
                           lambda argv: runner.run_logged(job_id, step, log, argv), log=log)
        except (im.ImageError, OSError) as e:
            log("!! %s" % e)
            return False
        log("Done. %s" % recipe.about if recipe.about else "Done.")
        log("The image's files: %s" % card)
    return True


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


def _queue(title, action, name, **more):
    from porthole.updater import runner
    job = runner.new_job("plugin", title, [dict({"kind": "plugin", "title": title,
                                                 "plugin": "crossdev", "action": action,
                                                 "name": name}, **more)], recheck=False)
    with runner.jobs_state() as state:
        state["jobs"].append(job)
        runner.prune(state)
    runner.start_runner()
    print(job["id"])
    return 0


def toolchain(args):
    """plugin crossdev toolchain NAME [again]: the toolchain its settings
    want (shared when one with the same gcc and binutils is built; again:
    build that one again anyway)"""
    from porthole.plugins.crossdev import roots
    if len(args) not in (1, 2) or args[1:] not in ([], ["again"]):
        print("usage: plugin crossdev toolchain NAME [again]", file=sys.stderr)
        return 2
    target = roots.made_here().get(args[0])
    if target is None:
        print("No system called %s was made here" % args[0], file=sys.stderr)
        return 1
    return _queue("Build the cross toolchain for %s" % target.title, "toolchain",
                  target.name, **({"again": True} if args[1:] else {}))


def _toolchain_in_use(tuple_, log=None):
    """The systems using a toolchain: noted against it, or wanting its gcc
    and binutils by their settings now"""
    from porthole.plugins.crossdev import roots, toolchain as tc
    systems = roots.made_here()
    using = set(tc.users(tuple_)) & set(systems)
    for name, target in sorted(systems.items()):
        try:
            found, _resolved = tc.for_system(target)
        except tc.ToolchainError:
            continue
        if found == tuple_:
            using.add(name)
    return sorted(using)


def remove_toolchain(args):
    """plugin crossdev remove-toolchain TUPLE: queue removing a toolchain
    no system uses (crossdev --clean)"""
    from porthole.plugins.crossdev import toolchain as tc
    from porthole.updater import runner
    if len(args) != 1 or not re.match(r"^[A-Za-z0-9_.]+(-[A-Za-z0-9_.]+){2,3}$", args[0]):
        print("usage: plugin crossdev remove-toolchain TUPLE", file=sys.stderr)
        return 2
    tuple_ = args[0]
    if tuple_ not in tc.read_registry()["toolchains"]:
        print("Porthole didn't make a toolchain called %s" % tuple_, file=sys.stderr)
        return 1
    if tuple_ == tc.host_chost():
        print("%s is this computer's own compiler" % tuple_, file=sys.stderr)
        return 1
    using = _toolchain_in_use(tuple_)
    if using:
        print("%s is used by %s" % (tuple_, ", ".join(using)), file=sys.stderr)
        return 1
    with runner.jobs_state(write=False) as state:
        busy = [j for j in runner.active_jobs(state)
                if any(s.get("plugin") == "crossdev" for s in j.get("steps") or [])]
    if busy:
        print("A system job is %s (%s): remove it once that has finished."
              % (busy[0]["status"], busy[0]["title"]), file=sys.stderr)
        return 1
    if not tc.installed(tuple_):
        tc.forget(tuple_)  # already gone from this computer: just off the list
        print("%s isn't installed any more; taken off the list" % tuple_)
        return 0
    return _queue("Remove the cross toolchain %s" % tuple_, "remove-toolchain", "",
                  toolchain=tuple_)


def step_remove_toolchain(runner, job_id, step, log):
    """crossdev --clean for a toolchain no system uses (checked again: a
    system may have taken it on since it was queued)"""
    import shutil
    from porthole.plugins.crossdev import toolchain as tc
    tuple_ = step.get("toolchain", "")
    using = _toolchain_in_use(tuple_)
    if using:
        log("!! %s is used by %s now; it stays" % (tuple_, ", ".join(using)))
        return False
    argv = ["crossdev", "--clean", tuple_]
    log("$ " + " ".join(argv))
    if not runner.run_logged(job_id, step, log, argv):
        return False
    unmask = os.path.join("/etc/portage/package.unmask", "porthole-cross-" + tuple_)
    if os.path.exists(unmask):
        os.unlink(unmask)
        log("Removed %s" % unmask)
    tc.forget(tuple_)
    if shutil.which("distcc-config"):
        log("$ distcc-config --update-masquerade-with-crossdev")
        runner.run_logged(job_id, step, log, ["distcc-config",
                                               "--update-masquerade-with-crossdev"])
    log("The toolchain %s is removed" % tuple_)
    return True


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
    # one toolchain per compiler: an installed one with the same gcc and
    # binutils is shared; otherwise a new one under a name of its own, so
    # the toolchains other systems use are never replaced
    existing = tc.find(resolved)
    if existing and not step.get("again"):
        tc.remember(existing, resolved, target.name)
        log("%s uses the toolchain %s: the same gcc and binutils its settings want are "
            "built there%s. Nothing to build." % (target.title, existing, (
                " (shared with %s)" % ", ".join(n for n in tc.users(existing)
                                                 if n != target.name))
                if len(tc.users(existing)) > 1 else ""))
        return True
    tuple_ = existing or tc.new_name(resolved)
    log("The toolchain %s's profile allows, for %s, built as %s:"
        % (target.title, resolved["tuple"], tuple_))
    for title, version, repo, built in tc.compare(resolved, tuple_=tuple_):
        log("   %-15s %-22s from %s%s" % (title, version, repo,
                                          "  (built: %s)" % built if built else ""))
    argv = tc.command(resolved, output, tuple_)
    # crossdev's settings first, then Porthole's unmask for the pinned
    # versions (crossdev's own "live" mask hides 9999.N ones), then a
    # check that all of them can be emerged, before hours of building
    log("$ " + " ".join(argv + ["--init-target"]))
    if not runner.run_logged(job_id, step, log, argv + ["--init-target"]):
        return False
    try:
        path = tc.write_unmask(resolved, tuple_=tuple_)
    except (tc.ToolchainError, OSError) as e:
        log("!! %s" % e)
        return False
    log("Unmasked exactly these versions in %s:" % path)
    for line in tc.unmask_lines(resolved, tuple_):
        log("   " + line)
    check = tc.pretend_command(resolved, tuple_)
    log("$ " + " ".join(check))
    if not runner.run_logged(job_id, step, log, check):
        log("!! The toolchain packages can't be emerged as they are (above); nothing "
            "was built.")
        return False
    log("$ " + " ".join(argv))
    if not runner.run_logged(job_id, step, log, argv):
        return False
    tc.remember(tuple_, resolved, target.name)
    log("%s builds with the toolchain %s from now on" % (target.title, tuple_))
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
    from porthole.plugins.crossdev import emulate, roots
    try:
        target = roots.made_here().get(step.get("name", ""))
        if target is not None:
            emulate.refuse_if_shell(target)
    except emulate.EmulateError as e:
        log("!! %s" % e)
        return False
    try:
        return roots.reset(step.get("name", ""), log=log)
    except (roots.RootError, OSError) as e:
        log("!! %s" % e)
        return False


def step_remove_root(runner, job_id, step, log):
    from porthole.plugins.crossdev import emulate, roots
    try:
        target = roots.made_here().get(step.get("name", ""))
        if target is not None:
            emulate.refuse_if_shell(target)
    except emulate.EmulateError as e:
        log("!! %s" % e)
        return False
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
        if os.path.isdir(os.path.join(target.root, "var/db/pkg")):
            # built already: the services take effect now (the rest is
            # written at the end of the next build)
            fb.apply_services(target, config, log=print)
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
        bd.write_policy(target, bool(changes.get("allow_python")), changes.get("host") or ())
    except (ValueError, bd.BuildError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    title = "Build %s" % target.title

    def step(action, step_title, **more):
        return dict({"kind": "plugin", "title": step_title, "plugin": "crossdev",
                     "action": action, "name": target.name}, **more)
    steps = [step("build-check", "Check the build can go ahead")]
    steps += [step("build-stage", stage_title, stage=stage) for stage, stage_title in bd.STAGES]
    steps.append(step("kernel", "Install the kernel inside the system"))
    steps.append(step("depclean", "Remove what nothing needs any more"))
    steps.append(step("firstboot", "The first-boot setup"))
    steps.append(step("health", "Check the finished system"))
    if changes.get("live_image"):
        steps.append(step("package", "Make its live image", recipe="livecd"))
    job = runner.new_job("plugin", title, steps, recheck=False)
    with runner.jobs_state() as state:
        state["jobs"].append(job)
        runner.prune(state)
    runner.start_runner()
    print(job["id"])
    return 0


def _build_env(target, log):
    from porthole.plugins.crossdev import build as bd, emulate
    try:
        emulate.refuse_if_shell(target)
    except emulate.EmulateError as e:
        raise bd.BuildError(str(e))
    chost = bd.chost_of(target)
    if not chost:
        raise bd.BuildError("%s's settings give no CHOST" % target.title)
    tc_name = None
    if chost != bd.host_chost():
        from porthole.plugins.crossdev import toolchain as tc
        try:
            tc_name, _resolved = tc.for_system(target)
        except tc.ToolchainError as e:
            raise bd.BuildError(str(e))
        if tc_name is None:
            raise bd.BuildError("There is no cross toolchain for what %s's settings want "
                                "yet: build its toolchain first (Systems window)"
                                % target.title)
        try:
            # which system uses which (an existing one is taken on without a job)
            if tc.read_registry()["systems"].get(target.name) != tc_name:
                tc.remember(tc_name, _resolved, target.name)
        except OSError as e:
            log("!! couldn't note which toolchain %s uses: %s" % (target.title, e))
    bd.write_wrappers(target, chost, toolchain=tc_name)
    # every system built from here (cross or not) needs its kernel part
    bd.write_bashrc(target, chost)
    if chost != bd.host_chost():
        bd.write_ldd(target, chost)
        bd.link_python_modules(target, log)
        bd.repair_perl_modules(target, log)
    return bd.environment(target, chost)


# what each step does, for the job's command record (recipe.py)
RECIPE_NOTES = {
    "toolchain": (
        "crossdev builds a cross toolchain: binutils, gcc, the kernel headers and the\n"
        "C library for another CPU, installed beside this computer's own compiler\n"
        "(as <CHOST>-gcc). Its ebuilds go into a local repository of their own."),
    "remove-toolchain": (
        "crossdev --clean removes a cross toolchain: its packages (category\n"
        "cross-<name>), its programs and its crossdev settings."),
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
        "  QEMU_LD_PREFIX      the system's libraries, for its programs run by qemu\n"
        "Every emerge into the system runs behind Porthole's sandbox (python -m\n"
        "porthole.plugins.crossdev.sandbox): in a mount namespace of its own, with this\n"
        "computer's /etc, /usr and /var as overlays, so what a package writes there\n"
        "outside the system's folders is caught and not made. Build tools it needs on\n"
        "this computer (the plan listed them) are installed first, by plain emerge."),
    ("build-stage", "base"): (
        "The kernel headers and the C library first: everything else is compiled\n"
        "against them. --usepkg installs a binary package already made with the same\n"
        "USE flags instead of compiling again. Before that, binary packages needing a\n"
        "newer glibc than the system gets (the GLIBC_2.x versions their programs and\n"
        "libraries name) are moved to <PKGDIR>.set-aside/<date>/ and the package\n"
        "index made again (emaint binhost --fix): portage would install them anyway."),
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
    "kernel": (
        "A kernel package installs its kernel (installkernel writes /boot, its\n"
        "hooks make the initramfs and the board's boot files) when it is merged. Into\n"
        "the system's folder that would run this computer's installkernel, so\n"
        "Porthole's bashrc notes the package instead, and here it is installed\n"
        "inside the system, through the emulator: emerge --config (the package's\n"
        "own pkg_config) with the system's installkernel, hooks and dracut."),
    "depclean": (
        "emerge --depclean: packages nothing in @system or @world needs any more (left\n"
        "behind when a package's dependencies or the system's USE flags change) are\n"
        "removed from the system. Their build dependencies are kept, as emerge does by\n"
        "default, and libraries something still links are kept (preserve-libs)."),
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
        try:
            tuple_, resolved = tc.for_system(target)
            tuple_ = tuple_ or chost
            record.note("Systems wanting the same gcc and binutils share one toolchain, under a\n"
                        "name of its own (%s); the wrappers above run its programs under\n"
                        "the system's names (%s-gcc, ...)." % (tuple_, chost))
            installed = tc.installed(tuple_)
            if installed:
                record.note("Installed: " + ", ".join("%s %s" % kv
                                                      for kv in sorted(installed.items())))
            output = checks.repos().get(checks.REPO_NAME)
            if output:
                record.note("Built with:\n  " + shlex.join(tc.command(resolved, output, tuple_)))
            unmask = os.path.join("/etc/portage/package.unmask", "porthole-cross-" + tuple_)
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
        tc_name, _resolved = tc.for_system(target)
        built = tc.installed(tc_name)
        sharing = [n for n in tc.users(tc_name) if n != target.name]
        log("Cross toolchain %s (as %s): %s%s" % (tc_name, chost, ", ".join(
            "%s %s" % kv for kv in sorted(built.items())) or "?",
            "; shared with " + ", ".join(sharing) if sharing else ""))
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
    from porthole.plugins.crossdev import health
    for line in health.account_mismatches(env["ROOT"]):
        log("!! Warning: %s. A package that sets file owners by name while it builds gets "
            "this computer's number; give the account the same number on both." % line)
    argv = bd.check_argv(target)
    log("$ " + " ".join(argv))
    if not _cross(runner, job_id, step, log, target, env, argv, installs=False):
        log("!! Portage can't work the build out as it stands (above); nothing was built.")
        return False
    return True


def _host_tools(runner, job_id, step, log, target, env, argv):
    """What argv would install on this computer (build tools), installed
    first, in a step of its own, when the user agreed to it in the plan;
    otherwise the build stops here. The emerge itself then has nothing to
    install outside the system (its sandbox would catch it)."""
    from porthole.plugins.crossdev import build as bd, emulate, sandbox
    from porthole.updater import recipe
    pretend = [a for a in argv if not a.startswith("--color")] + ["--pretend", "--color=n"]
    result = subprocess.run(pretend, capture_output=True, text=True,
                            env=dict(os.environ, **env))
    recipe.ran(pretend, result.returncode)
    host = bd.parse_pretend(result.stdout, target.root)["host"]
    if not host:
        # nothing for here; or emerge can't work it out, which the emerge
        # itself shows
        return True
    approved = bd.approved_host(target)
    unasked = [cpv for cpv in host if emulate.cp_of(cpv) not in approved]
    if unasked:
        log("!! This would install on this computer what the build plan didn't list, so "
            "you haven't agreed to it: %s. Nothing was installed. Starting the build "
            "again shows the new plan to agree to." % " ".join(unasked))
        raise sandbox.Stopped()
    argv = ["emerge", "--verbose", "--color=y", "--oneshot"] + ["=" + cpv for cpv in host]
    log("== On this computer first: %d build tool%s the plan listed (they run here "
        "while the system's packages build)" % (len(host), "" if len(host) == 1 else "s"))
    log("$ " + " ".join(argv))
    if not runner.run_logged(job_id, step, log, argv):
        log("!! The build tools for this computer didn't install (above)")
        raise sandbox.Stopped()
    return True


def _cross(runner, job_id, step, log, target, env, argv, installs=True):
    """An emerge (or other portage command) into the system: the build
    tools it needs here first (installs), then it, in the sandbox"""
    from porthole.plugins.crossdev import sandbox
    if installs:
        _host_tools(runner, job_id, step, log, target, env, argv)
    return sandbox.run(runner, job_id, step, log, argv, target, env)


def _stops(action):
    """A step that fails as it is when the build tried to change this
    computer (sandbox.Stopped, already logged)"""
    def step(runner, job_id, step_, log):
        from porthole.plugins.crossdev import sandbox
        try:
            return action(runner, job_id, step_, log)
        except sandbox.Stopped:
            return False
    step.__doc__ = action.__doc__
    return step


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


def _system_left(env, wanted):
    """[=cat/pkg-ver] the system set still needs, with what they need, as a
    cross build would merge them; set members whose plan takes in any of
    wanted (cat/pkg) are left out (nintendo-switch-meta needing glib)"""
    from porthole.plugins.crossdev import emulate
    from porthole.updater import recipe

    def plan(args):
        argv = ["emerge", "--pretend", "--oneshot", "--noreplace", "--color=n"] + args
        result = subprocess.run(argv, capture_output=True, text=True,
                                env=dict(os.environ, **env))
        recipe.ran(argv, result.returncode)
        return emulate.merge_list(result.stdout) if result.returncode == 0 else None

    def clean(found):
        return found is not None and not any(emulate.cp_of(a) in wanted for a in found)

    members = [a for a in plan(["--nodeps", "@system"]) or []
               if emulate.cp_of(a) not in wanted]
    if not members:
        return []
    found = plan(members)
    if clean(found):
        return found
    left = []
    for member in members:
        found = plan([member])
        if clean(found):
            left += [a for a in found if a not in left]
    return left


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
        _cross(runner, job_id, step, log, target, env, argv)
        still = [cp for cp in emulate.NEEDED if not emulate.installed(root, cp)]
        if still:
            log("!! %s didn't build; can't build in the emulator without %s"
                % (" and ".join(still), "it" if len(still) == 1 else "them"))
            return False
    helpers = Helpers(job_id, ["binfmt", "distcc"],
                      distcc={"path": bd.wrappers_dir(target)})
    bd.distcc_names(bd.chost_of(target))
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
        # ebuilds take the system set as given (flex's configure needs grep),
        # and the stage that would have built the rest of it stopped at what
        # failed: what of it doesn't need them is cross-built with the tools
        base = _system_left(env, wanted)
        if base:
            log("== emulator: %d package%s of the system set (and what they need) not in "
                "yet, cross-built with the tools" % (len(base), "" if len(base) == 1 else "s"))
        tools = base + [a for a in tools if a not in base]
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
                if _cross(runner, job_id, step, log, target, env, argv):
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
        emulate.drop_distcc_links(root, log)
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
        if stage == "base":
            _set_aside_newer_libc(runner, job_id, step, log, target, env)
        result = _run_stage(runner, job_id, step, log, target, stage, env, argvs)
        if not (runner.update(job_id) or {}).get("cancel"):
            _redo_binpkgs(runner, job_id, step, log, target, env)
        return result
    finally:
        helpers.stop(log)


def _set_aside_newer_libc(runner, job_id, step, log, target, env):
    """Before anything is installed: the system's binary packages are tested
    against the glibc it gets (its settings' choice), and those needing a
    newer one (made while it had one, say before a board's repository took
    a glibc version back) are moved out of its package folder, so emerge
    builds them again instead of installing what wouldn't link or run.
    Moved, not deleted: <PKGDIR>.set-aside/<date>/."""
    import glob
    import shutil
    import time
    from porthole.plugins.crossdev import health as hc, toolchain as tc
    try:
        resolved = tc.resolve(target)
    except tc.ToolchainError as e:
        log("!! The binary packages weren't tested against the system's C library: %s" % e)
        return
    libc = next((p for p in resolved["parts"] if p["part"] == "libc"), None)
    if libc is None or libc["cp"] != tc.LIBCS["glibc"]:
        return
    wanted = hc.libc_version(libc["version"])
    root = os.path.realpath(target.root)
    have = [os.path.basename(d) for d in glob.glob(os.path.join(root, "var/db/pkg",
                                                                 libc["cp"] + "-[0-9]*"))]
    for pf in have:
        version = pf[len("glibc-"):]
        if hc.libc_version(version) > wanted:
            log("!! Warning: the system has glibc %s, newer than its settings give it now "
                "(%s). glibc can't be taken back a version in place: Start over (its "
                "settings are kept) to build it with %s." % (version, libc["version"],
                                                             libc["version"]))
    try:
        pkgdir, newer = hc.newer_libc_binpkgs(target, wanted, log=log)
    except Exception as e:
        log("!! The binary packages couldn't be tested against glibc %s: %s"
            % (libc["version"], e))
        return
    if not newer:
        return
    aside = os.path.join(pkgdir.rstrip("/") + ".set-aside", time.strftime("%Y%m%d-%H%M%S"))
    log("== %d binary package%s need%s a newer glibc than the system gets (%s): moved to "
        "%s, so they are built again" % (len(newer), "" if len(newer) == 1 else "s",
                                         "s" if len(newer) == 1 else "",
                                         libc["version"], aside))
    for rel, need in newer:
        log("   %s (needs glibc %s)" % (rel, need))
        try:
            os.makedirs(os.path.dirname(os.path.join(aside, rel)), exist_ok=True)
            shutil.move(os.path.join(pkgdir, rel), os.path.join(aside, rel))
        except OSError as e:
            log("!! couldn't move %s: %s" % (rel, e))
    argv = ["emaint", "binhost", "--fix"]
    log("$ " + " ".join(argv))
    _cross(runner, job_id, step, log, target, env, argv, installs=False)


def _installed_pf(root, rel):
    """cat/pf installed in the system that a binary package (relative to
    PKGDIR: cat/pn/pf-N.gpkg.tar, cat/pf.gpkg.tar, cat/pf.tbz2 ...) was made
    of, or None"""
    parts = rel.split("/")
    base = re.sub(r"\.(gpkg\.tar|tbz2|xpak)$", "", parts[-1])
    for pf in (base, re.sub(r"-\d+$", "", base)):
        if os.path.isdir(os.path.join(root, "var/db/pkg", parts[0], pf)):
            return "%s/%s" % (parts[0], pf)
    return None


def _redo_binpkgs(runner, job_id, step, log, target, env):
    """Binary packages made with this computer's perl folders or Python
    endings (the hook in the system's bashrc couldn't ask the system's perl
    or Python: seen when it came in with the same emerge), made again from
    the installed copies, which the hook fixed when they were merged
    (quickpkg). One not installed is only removed: building it makes it
    again."""
    from porthole.plugins.crossdev import health as hc
    root = os.path.realpath(target.root)
    try:
        pkgdir, bad = hc.bad_binpkgs(target, root)
    except Exception as e:
        log("!! The binary packages couldn't be checked: %s" % e)
        return
    if not bad:
        return
    log("== %d binary package%s made with this computer's perl folders or Python "
        "endings: made again from what is installed" % (len(bad), "" if len(bad) == 1
                                                         else "s"))
    redo = []
    for rel in bad:
        pf = _installed_pf(root, rel)
        try:
            os.unlink(os.path.join(pkgdir, rel))
        except OSError as e:
            log("!! couldn't remove %s: %s" % (rel, e))
            continue
        if pf:
            redo.append("=" + pf)
        else:
            log("== %s isn't installed: removed, building it makes it again" % rel)
    if redo:
        # settings files as installed go in, as in a built package; ones
        # changed since (quickpkg's precaution) don't
        argv = ["quickpkg", "--include-unmodified-config=y"] + redo
        log("$ " + " ".join(argv))
        if not _cross(runner, job_id, step, log, target, env, argv, installs=False):
            log("!! quickpkg didn't make them all (above); the health check lists what's "
                "left")
    argv = ["emaint", "binhost", "--fix"]
    log("$ " + " ".join(argv))
    _cross(runner, job_id, step, log, target, env, argv, installs=False)


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
            ok = _cross(runner, job_id, step, log, target, env, argv)
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
        if _cross(runner, job_id, step, log, target, env, argv):
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
    # configuration file updates its packages left (._cfg): the ones that
    # need no decision settled, the rest reported by the check below
    from porthole.updater import configupdates
    try:
        done, _left = configupdates.auto(target, log=lambda m: log("   " + m))
        if done:
            log(configupdates.report(target, done, []))
    except Exception as e:
        log("!! configuration file updates couldn't be checked: %s" % e)
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


def step_depclean(runner, job_id, step, log):
    """Remove what nothing in the system needs any more, after @world (mesa
    once nothing asks for it). emerge refuses, removing nothing, when the
    system's dependencies don't work out"""
    from porthole.plugins.crossdev import build as bd, roots
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    try:
        env = _build_env(target, log)
    except (bd.BuildError, OSError) as e:
        log("!! %s" % e)
        return False
    argv = ["emerge", "--depclean", "--verbose", "--color=y"]
    log("$ " + " ".join(argv))
    if not _cross(runner, job_id, step, log, target, env, argv, installs=False):
        log("!! emerge couldn't work out what the system still needs (above); nothing "
            "was removed.")
        return False
    return True


def step_kernel(runner, job_id, step, log):
    """Kernel packages merged into the system didn't install their kernel
    (Porthole's bashrc noted them, see build.kernel_block): installed here
    inside the system, emerge --config (the package's own pkg_config, i.e.
    its pkg_postinst again) in the emulator chroot, so the system's
    installkernel, its hooks and dracut write /boot"""
    from porthole.plugins.crossdev import build as bd, emulate, roots
    from porthole.updater.ondemand import Helpers, HelperError
    target = roots.made_here().get(step.get("name", ""))
    if target is None:
        log("!! No system called %s was made here" % step.get("name"))
        return False
    root = os.path.realpath(target.root)
    waiting = bd.kernels_waiting(root)
    if not waiting:
        log("No kernel waiting to be installed.")
        return True
    for cpf in list(waiting):
        if not os.path.isdir(os.path.join(root, "var/db/pkg", cpf)):
            log("%s isn't in the system any more: nothing to install" % cpf)
            bd.kernel_installed(root, cpf)
            waiting.remove(cpf)
    if not waiting:
        return True
    try:
        emulate.refuse_if_shell(target)
    except emulate.EmulateError as e:
        log("!! %s" % e)
        return False
    # installkernel's hooks (dracut) run the system's programs: its library
    # cache first
    if not emulate.ldconfig(runner, job_id, step, log, target):
        return False
    chost = bd.chost_of(target)
    helpers = Helpers(job_id, ["binfmt"] if chost != bd.host_chost() else [])
    try:
        helper_env = helpers.start(log)
    except HelperError as e:
        log("!! %s" % e)
        return False
    ok = True
    try:
        inside = emulate.emulated_env(target, helper_env)
        with emulate.Chroot(target, log) as chroot:
            for cpf in waiting:
                argv = chroot.emerge_argv(["--config", "=" + cpf], inside)
                log("== installing %s's kernel inside the system" % cpf)
                log("$ " + " ".join(argv[argv.index("emerge"):]))
                if runner.run_logged(job_id, step, log, argv):
                    bd.kernel_installed(root, cpf)
                else:
                    log("!! %s's kernel didn't install (above); the next build tries again"
                        % cpf)
                    ok = False
    except (emulate.EmulateError, OSError) as e:
        log("!! The emulator couldn't be set up: %s" % e)
        return False
    finally:
        helpers.stop(log)
    if ok:
        boot = sorted(os.listdir(os.path.join(root, "boot"))) if os.path.isdir(
            os.path.join(root, "boot")) else []
        log("The system's /boot: %s" % (" ".join(boot) or "(empty)"))
    return ok


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


COMMANDS = {"check": check, "health": health, "shell": shell,
            "remove-toolchain": remove_toolchain, "make-repo": make_repo, "new-root": new_root,
            "remove-root": remove_root, "reset-root": reset_root, "toolchain": toolchain,
            "binpkgs": binpkgs,
            "settings": settings, "write-file": write_file, "profile": profile,
            "firstboot": firstboot, "packages": packages, "build": build,
            "export": export, "images": images, "package": package}
STEPS = {"remove-root": step_remove_root, "reset-root": step_reset_root,
         "toolchain": step_toolchain,
         "firstboot": step_firstboot, "build-check": _stops(step_build_check),
         "build-stage": _stops(step_build_stage), "emulate": _stops(step_emulate),
         "break-cycle": _stops(step_break_cycle), "health": step_health,
         "depclean": _stops(step_depclean), "kernel": step_kernel,
         "remove-toolchain": step_remove_toolchain, "package": step_package}
