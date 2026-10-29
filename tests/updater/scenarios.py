#!/usr/bin/env python3
"""
    Hard update scenarios for the analyzer, built from fake packages.

    Each scenario is a tiny repository of ebuilds plus a fake installed
    system (ROOT with its own vdb and world file). The analyzer resolves
    a world update against it; nothing is built. Runs as a normal user.

    Usage: scenarios.py [--keep] [NAME...]     (default: all)

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pkg(cpv, slot="0", iuse="", depend="", rdepend="", keywords="amd64"):
    return {"cpv": cpv, "slot": slot, "iuse": iuse, "depend": depend,
            "rdepend": rdepend, "keywords": keywords}


def inst(cpv, use="", **kw):
    """An installed package; kw as for pkg (deps as recorded at build)"""
    p = pkg(cpv, **kw)
    p["use"] = use
    return p


# -- scenarios: available ebuilds, installed packages, world -----------------

CYCLES = {
    "doc": "Two build-time cycles that only a temporary USE change breaks",
    "repo": [
        pkg("lib/x-1"), pkg("lib/x-2", iuse="+y", depend="y? ( >=lib/y-2 )"),
        pkg("lib/y-1"), pkg("lib/y-2", depend=">=lib/x-2"),
        pkg("media/p-1"), pkg("media/p-2", iuse="+q", depend="q? ( >=media/q-2 )"),
        pkg("media/q-1"), pkg("media/q-2", depend=">=media/p-2"),
    ],
    "installed": [
        inst("lib/x-1"), inst("lib/y-1"), inst("media/p-1"), inst("media/q-1"),
    ],
    "world": ["lib/x", "lib/y", "media/p", "media/q"],
}

OLD_DEP = {
    "doc": "An installed package with no ebuild left pins <lib/z-2; "
           "a world package needs >=lib/z-2",
    "repo": [
        pkg("lib/z-1"), pkg("lib/z-2"),
        pkg("app/newthing-1"), pkg("app/newthing-2", rdepend=">=lib/z-2"),
    ],
    "installed": [
        inst("lib/z-1"), inst("app/newthing-1"),
        inst("app-old/legacy-1", rdepend="<lib/z-2"),
    ],
    "world": ["app/newthing", "app-old/legacy"],
}

SUBSLOT = {
    "doc": "lib/abi changes ABI (subslot 0/1 -> 0/2); app/cons can be rebuilt, "
           "app/frozen (no ebuild left) can't",
    "repo": [
        pkg("lib/abi-1.0", slot="0/1"), pkg("lib/abi-2.0", slot="0/2"),
        pkg("app/cons-1", rdepend="lib/abi:=", depend="lib/abi:="),
    ],
    "installed": [
        inst("lib/abi-1.0", slot="0/1"),
        inst("app/cons-1", rdepend="lib/abi:0/1=", depend="lib/abi:0/1="),
        inst("app/frozen-1", rdepend="lib/abi:0/1="),
    ],
    "world": ["app/cons", "app/frozen"],
}

FIXES = {
    "doc": "The update needs a ~amd64 version and a USE change on a dependency",
    "repo": [
        pkg("lib/k-2"), pkg("lib/k-3", keywords="~amd64"),
        pkg("lib/feat-1", iuse="extra"),
        pkg("app/tool-1"), pkg("app/tool-2", rdepend=">=lib/k-3 lib/feat[extra]"),
    ],
    "installed": [inst("lib/k-2"), inst("lib/feat-1", iuse="extra"), inst("app/tool-1")],
    "world": ["app/tool"],
}

BLOCKER = {
    "doc": "app/conflict-2 blocks app/oldtool, which is in world",
    "repo": [
        pkg("app/conflict-1"), pkg("app/conflict-2", rdepend="!app/oldtool"),
        pkg("app/oldtool-1"),
    ],
    "installed": [inst("app/conflict-1"), inst("app/oldtool-1")],
    "world": ["app/conflict", "app/oldtool"],
}


def combine(*scenarios):
    return {
        "doc": "All of the above at once",
        "repo": [p for s in scenarios for p in s["repo"]],
        "installed": [p for s in scenarios for p in s["installed"]],
        "world": [w for s in scenarios for w in s["world"]],
    }


SCENARIOS = {
    "cycles": CYCLES,
    "old-dep": OLD_DEP,
    "subslot": SUBSLOT,
    "fixes": FIXES,
    "blocker": BLOCKER,
    "everything": combine(CYCLES, OLD_DEP, SUBSLOT, FIXES, BLOCKER),
}


# -- building the fake system ------------------------------------------------

def split(cpv):
    cat, pf = cpv.split("/")
    name, ver = pf.rsplit("-", 1)
    return cat, name, ver, pf


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def build(work, scenario):
    repo = os.path.join(work, "repo")
    config = os.path.join(work, "config")
    root = os.path.join(work, "root")

    write(os.path.join(repo, "profiles", "repo_name"), "scenario\n")
    write(os.path.join(repo, "metadata", "layout.conf"),
          "masters =\nthin-manifests = true\nsign-manifests = false\n")
    write(os.path.join(repo, "profiles", "categories"),
          "\n".join(sorted({split(p["cpv"])[0] for p in
                            scenario["repo"] + scenario["installed"]})) + "\n")
    write(os.path.join(repo, "profiles", "arch.list"), "amd64\n")
    write(os.path.join(repo, "profiles", "test", "eapi"), "8\n")
    write(os.path.join(repo, "profiles", "test", "make.defaults"),
          'ARCH="amd64"\nACCEPT_KEYWORDS="amd64"\nCHOST="x86_64-pc-linux-gnu"\n'
          'ELIBC="glibc"\nKERNEL="linux"\nUSERLAND="GNU"\n')
    for p in scenario["repo"]:
        cat, name, ver, pf = split(p["cpv"])
        write(os.path.join(repo, cat, name, pf + ".ebuild"),
              'EAPI=8\nDESCRIPTION="scenario package"\nHOMEPAGE="https://example.org"\n'
              'LICENSE="MIT"\nSLOT="%(slot)s"\nKEYWORDS="%(keywords)s"\n'
              'IUSE="%(iuse)s"\nDEPEND="%(depend)s"\nRDEPEND="%(rdepend)s"\n'
              'S="${WORKDIR}"\n' % p)

    etc = os.path.join(config, "etc", "portage")
    write(os.path.join(etc, "make.conf"),
          'ACCEPT_LICENSE="*"\nFEATURES="-news"\n'
          'PORTAGE_TMPDIR="%s"\nDISTDIR="%s"\nPKGDIR="%s"\n'
          % (os.path.join(work, "tmp"), os.path.join(work, "distfiles"),
             os.path.join(work, "packages")))
    write(os.path.join(etc, "repos.conf", "scenario.conf"),
          "[DEFAULT]\nmain-repo = scenario\n\n[scenario]\nlocation = %s\n"
          "auto-sync = no\n" % repo)
    os.symlink(os.path.join(repo, "profiles", "test"), os.path.join(etc, "make.profile"))
    for d in ("package.use", "package.accept_keywords", "package.unmask",
              "package.license", "package.mask"):
        os.makedirs(os.path.join(etc, d))
    os.makedirs(os.path.join(work, "tmp"))

    for i, p in enumerate(scenario["installed"], 1):
        cat, name, ver, pf = split(p["cpv"])
        vdb = os.path.join(root, "var", "db", "pkg", cat, pf)
        fields = {
            "CATEGORY": cat, "PF": pf, "SLOT": p["slot"], "EAPI": "8",
            "KEYWORDS": p["keywords"], "IUSE": p["iuse"],
            "USE": " ".join(["amd64", "elibc_glibc", "kernel_linux"] + p["use"].split()),
            "DEPEND": p["depend"], "RDEPEND": p["rdepend"], "BDEPEND": "", "PDEPEND": "",
            "repository": "scenario", "COUNTER": str(i), "BUILD_TIME": "1700000000",
            "CHOST": "x86_64-pc-linux-gnu", "LICENSE": "MIT", "DEFINED_PHASES": "",
            "CONTENTS": "",
        }
        for key, value in fields.items():
            write(os.path.join(vdb, key), value + "\n" if value else "")
    write(os.path.join(root, "var", "lib", "portage", "world"),
          "\n".join(scenario["world"]) + "\n")
    return config, root


def run(name, keep=False):
    scenario = SCENARIOS[name]
    work = tempfile.mkdtemp(prefix="porthole-scenario-%s-" % name)
    try:
        config, root = build(work, scenario)
        out = os.path.join(work, "plan.json")
        env = dict(os.environ, PYTHONPATH=SRC)
        proc = subprocess.run(
            [sys.executable, "-m", "porthole.updater.analyze", "--json", out,
             "--config-root", config, "--",
             "--update", "--deep", "--newuse", "@world",
             "--root=" + root, "--sysroot=" + root],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=1800)
        print("=" * 72)
        print("%s: %s" % (name, scenario["doc"]))
        print("=" * 72)
        print(proc.stdout)
        with open(out) as f:
            return json.load(f)
    finally:
        if keep:
            print("(kept %s)" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)


def main(argv):
    keep = "--keep" in argv
    names = [a for a in argv if not a.startswith("-")] or list(SCENARIOS)
    for name in names:
        run(name, keep)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
