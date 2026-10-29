"""
    Porthole update resolver worker

    Runs one portage dependency resolution (the same code path as
    `emerge --pretend`) and writes everything it learned as JSON:
    the merge list, needed config changes, slot conflicts, circular
    dependencies, blockers and unsatisfied dependencies.

    Each resolution runs in its own process (portage keeps global
    state), so this module only imports portage and the stdlib.

    Usage:
        python3 -m porthole.updater.resolve OUTPUT.json [emerge args...]

    Set PORTAGE_CONFIGROOT to resolve against a scratch copy of
    /etc/portage.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import os
import sys
import time
import traceback

import portage
from _emerge.Package import Package
from _emerge.actions import adjust_configs, load_emerge_config
from _emerge.create_depgraph_params import create_depgraph_params
from _emerge.depgraph import backtrack_depgraph
from _emerge.main import parse_opts
from _emerge.resolver.circular_dependency import circular_dependency_handler
from _emerge.stdout_spinner import stdout_spinner


def pkg_info(pkg):
    """Return a JSON friendly description of a Package"""
    return {
        "cpv": str(pkg.cpv),
        "cp": pkg.cp,
        "version": pkg.version,
        "slot": pkg.slot,
        "repo": pkg.repo,
        "installed": pkg.installed,
        "operation": pkg.operation,
    }


def parents_of(depgraph, pkg):
    """List what pulled a package in, as (parent cpv, atom) pairs"""
    parents = []
    for parent, atom in depgraph._dynamic_config._parent_atoms.get(pkg, ()):
        name = str(parent.cpv) if isinstance(parent, Package) else str(parent)
        atom = getattr(atom, "unevaluated_atom", atom)
        parents.append({"parent": name, "atom": str(atom)})
    return sorted(parents, key=lambda p: (p["parent"], p["atom"]))


def unstable_keyword(pkg, settings):
    """The keyword emerge would suggest accepting for pkg"""
    arch = settings.get("ARCH", "")
    keywords = pkg._metadata.get("KEYWORDS", "").split()
    if "~" + arch in keywords:
        return "~" + arch
    if not keywords or keywords == ["-*"]:
        return "**"
    return "~" + arch if arch else "**"


def needed_changes(depgraph, settings):
    """Config changes (autounmask) portage needs to proceed"""
    dc = depgraph._dynamic_config
    graph = dc.digraph
    keywords = [
        dict(pkg_info(pkg), keyword=unstable_keyword(pkg, settings))
        for pkg in dc._needed_unstable_keywords
        if pkg in graph
    ]
    unmask = [pkg_info(pkg) for pkg in dc._needed_p_mask_changes if pkg in graph]
    licenses = [
        dict(pkg_info(pkg), licenses=sorted(lic))
        for pkg, lic in dc._needed_license_changes.items()
        if pkg in graph
    ]
    use = []
    for pkg, (new_use, changes) in dc._needed_use_config_changes.items():
        if pkg in graph:
            use.append(
                dict(
                    pkg_info(pkg),
                    changes={flag: bool(state) for flag, state in changes.items()},
                    parents=parents_of(depgraph, pkg),
                )
            )
    key = lambda p: p["cpv"]
    return {
        "keywords": sorted(keywords, key=key),
        "unmask": sorted(unmask, key=key),
        "licenses": sorted(licenses, key=key),
        "use": sorted(use, key=key),
    }


def slot_conflicts(depgraph):
    """Packages that want different versions in the same slot"""
    conflicts = []
    for conflict in depgraph._dynamic_config._package_tracker.slot_conflicts():
        conflicts.append(
            {
                "slot_atom": str(conflict.atom),
                "packages": [
                    dict(pkg_info(pkg), parents=parents_of(depgraph, pkg))
                    for pkg in sorted(conflict.pkgs)
                ],
            }
        )
    return conflicts


def circular_deps(depgraph):
    """Dependency cycles plus portage's USE suggestions for breaking them"""
    graph = getattr(depgraph._dynamic_config, "_circular_deps_for_display", None)
    if graph is None:
        return None
    handler = circular_dependency_handler(depgraph, graph)
    solutions = []
    for pkg, pkg_solutions in (handler.solutions or {}).items():
        for solution in pkg_solutions:
            solutions.append(
                {
                    "breaks_at": str(pkg.cpv),
                    "changes": {flag: state for flag, state in solution},
                }
            )
    # the suggestion names the parent whose USE must change
    parents = []
    for line in handler.suggestions or ():
        parents.append(line.split("(Change USE")[0].strip("- \n"))
    for sol, parent in zip(solutions, parents):
        sol["package"] = parent
    return {
        "cycle": [str(p.cpv) for p in handler.shortest_cycle or ()],
        "merge_list": [str(p.cpv) for p in handler.merge_list or ()],
        "solutions": solutions,
        "message": handler.circular_dep_message,
    }


def blockers(depgraph):
    found = []
    for blocker in getattr(
        depgraph._dynamic_config, "_unsatisfied_blockers_for_display", None
    ) or ():
        found.append(
            {
                "atom": str(blocker.atom),
                "parent": str(blocker.parent.cpv),
            }
        )
    return found


def unsatisfied(depgraph):
    found = []
    for (root, atom), info in depgraph._dynamic_config._unsatisfied_deps_for_display:
        parent = info.get("myparent")
        found.append(
            {
                "atom": str(atom),
                "parent": str(parent.cpv) if isinstance(parent, Package) else None,
            }
        )
    return found


def merge_list(depgraph):
    """Packages the resolver would merge, in graph order when possible"""
    try:
        tasks = depgraph.altlist()
    except Exception:  # unresolved graph; fall back to the raw digraph
        tasks = list(depgraph._dynamic_config.digraph)
    pkgs = [
        t for t in tasks if isinstance(t, Package) and t.operation == "merge"
    ]
    out = []
    for pkg in pkgs:
        info = pkg_info(pkg)
        vardb = depgraph._frozen_config.trees[pkg.root]["vartree"].dbapi
        old = vardb.match(pkg.slot_atom)
        info["replaces"] = [portage.versions.cpv_getversion(c) for c in old]
        if old:
            cmp = portage.versions.vercmp(pkg.version, info["replaces"][0])
            info["kind"] = "update" if cmp > 0 else "downgrade" if cmp < 0 else "rebuild"
        elif vardb.match(pkg.cp):
            info["kind"] = "new-slot"
        else:
            info["kind"] = "new"
        info["parents"] = parents_of(depgraph, pkg)
        # 32-bit builds need the kernel's IA32 emulation
        info["abi_x86_32"] = "abi_x86_32" in pkg.use.enabled
        out.append(info)
    return out


def module_packages(vardb):
    """Installed packages that put files in /lib/modules (@module-rebuild),
    leaving out the kernels themselves"""
    found = []
    for cpv in vardb.cpv_all():
        if cpv.startswith("sys-kernel/"):
            continue
        try:
            with open(vardb.getpath(cpv, filename="CONTENTS"), errors="replace") as f:
                if any(line.split(" ", 2)[1].startswith("/lib/modules/")
                       for line in f if " " in line):
                    found.append(str(cpv))
        except OSError:
            pass
    return sorted(found)


def preserved_libs(vardb):
    """Old libraries portage kept because installed programs still use
    them, as {owner cpv: [files]}; None if the registry can't be read"""
    registry = getattr(vardb, "_plib_registry", None)
    if registry is None:
        return None
    try:
        registry.load()
    except portage.exception.PortageException:
        return None  # unreadable when not run as root
    return {str(cpv): sorted(files)
            for cpv, files in registry.getPreservedLibs().items()}


def system_info(depgraph, settings):
    """Facts the planner needs about the target system"""
    root = settings["EROOT"]
    vardb = depgraph._frozen_config.trees[root]["vartree"].dbapi
    return {
        "chost": settings.get("CHOST", ""),
        "build_flags": {k: settings.get(k, "") for k in ("CFLAGS", "CXXFLAGS", "LDFLAGS")},
        "installed_count": len(vardb.cpv_all()),
        "module_packages": module_packages(vardb),
        "multilib_abis": settings.get("MULTILIB_ABIS", "").split(),
        # the depgraph's vartree is a FakeVartree without the registry
        "preserved_libs": preserved_libs(
            depgraph._frozen_config._trees_orig[root]["vartree"].dbapi),
    }


def resolve(emerge_args):
    """Run one resolution and return the result dict"""
    started = time.monotonic()
    myaction, myopts, myfiles = parse_opts(emerge_args, silent=True)
    myopts["--pretend"] = True
    myopts["--quiet"] = True
    # emerge's --root/--config-root work through the environment
    if "--root" in myopts:
        os.environ["ROOT"] = myopts["--root"]
    if "--sysroot" in myopts:
        os.environ["SYSROOT"] = myopts["--sysroot"]
    if "--config-root" in myopts:
        os.environ["PORTAGE_CONFIGROOT"] = myopts["--config-root"]
    emerge_config = load_emerge_config(action=myaction, args=myfiles, opts=myopts)
    adjust_configs(myopts, emerge_config.trees)
    settings = emerge_config.target_config.settings
    trees = emerge_config.trees
    spinner = stdout_spinner()
    spinner.mode = spinner.QUIET
    params = create_depgraph_params(myopts, myaction)

    success, depgraph, favorites = backtrack_depgraph(
        settings, trees, myopts, params, myaction, myfiles, spinner
    )
    # display_problems() fills in the circular dependency and blocker data
    depgraph._show_merge_list = lambda *a, **k: None
    if not success:
        with open(os.devnull, "w") as devnull:
            stdout, stderr = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = devnull
            try:
                depgraph.display_problems()
            except Exception:
                pass
            finally:
                sys.stdout, sys.stderr = stdout, stderr

    result = {
        "args": emerge_args,
        "config_root": settings["PORTAGE_CONFIGROOT"],
        "success": bool(success),
        "seconds": round(time.monotonic() - started, 1),
        "errors": {},
    }
    # a failure in one section must not lose the others
    sections = (
        ("merge_list", lambda: merge_list(depgraph)),
        ("needed_changes", lambda: needed_changes(depgraph, settings)),
        ("slot_conflicts", lambda: slot_conflicts(depgraph)),
        ("circular", lambda: circular_deps(depgraph)),
        ("blockers", lambda: blockers(depgraph)),
        ("unsatisfied", lambda: unsatisfied(depgraph)),
        ("system", lambda: system_info(depgraph, settings)),
    )
    for name, extract in sections:
        try:
            result[name] = extract()
        except Exception:
            result[name] = None
            result["errors"][name] = traceback.format_exc()
    return result


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    output = argv[1]
    try:
        result = resolve(argv[2:])
    except Exception as e:
        result = {"args": argv[2:], "error": repr(e), "traceback": traceback.format_exc()}
    with open(output, "w") as f:
        json.dump(result, f, indent=1)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
