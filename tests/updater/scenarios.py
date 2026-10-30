#!/usr/bin/env python3
"""
    Hard update scenarios for the analyzer, built from fake packages.

    Each scenario is a tiny repository of ebuilds plus a fake installed
    system (ROOT with its own vdb and world file). The analyzer resolves
    a world update against it; nothing is built. Runs as a normal user.

    Each scenario's plan is checked against EXPECTED; the exit status
    says whether they all came out as expected.

    Usage: scenarios.py [--keep] [-q] [NAME...]     (default: all)

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


def pkg(cpv, slot="0", iuse="", depend="", rdepend="", keywords="amd64", license="MIT"):
    return {"cpv": cpv, "slot": slot, "iuse": iuse, "depend": depend,
            "rdepend": rdepend, "keywords": keywords, "license": license}


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

HARD_BLOCKER = {
    "doc": "app/strict-2 hard-blocks (!!) app/legacytool, which is in world; "
           "portage won't replace it during the merge",
    "repo": [
        pkg("app/strict-1"), pkg("app/strict-2", rdepend="!!app/legacytool"),
        pkg("app/legacytool-1"),
    ],
    "installed": [inst("app/strict-1"), inst("app/legacytool-1")],
    "world": ["app/strict", "app/legacytool"],
}


HELD_NEWER = {
    "doc": "app/user-1 (still in the tree) requires <lib/w-2; only app/user-2, "
           "~amd64, works with lib/w-2, which app/wnew-2 needs",
    "repo": [
        pkg("lib/w-1"), pkg("lib/w-2"),
        pkg("app/user-1", rdepend="<lib/w-2"), pkg("app/user-2", keywords="~amd64"),
        pkg("app/wnew-1"), pkg("app/wnew-2", rdepend=">=lib/w-2"),
    ],
    "installed": [inst("lib/w-1"), inst("app/user-1", rdepend="<lib/w-2"),
                  inst("app/wnew-1")],
    "world": ["app/user", "app/wnew"],
}

HELD_STUCK = {
    "doc": "app/stuck-1 (still in the tree, no newer version) requires <lib/v-2, "
           "which app/vnew-2 needs",
    "repo": [
        pkg("lib/v-1"), pkg("lib/v-2"),
        pkg("app/stuck-1", rdepend="<lib/v-2"),
        pkg("app/vnew-1"), pkg("app/vnew-2", rdepend=">=lib/v-2"),
    ],
    "installed": [inst("lib/v-1"), inst("app/stuck-1", rdepend="<lib/v-2"),
                  inst("app/vnew-1")],
    "world": ["app/stuck", "app/vnew"],
}


# installs from Porthole's queue: planned from a request, not @world
INSTALL_PKGS = [pkg("lib/l-1"), pkg("app/a-1"), pkg("app/b-1", rdepend="lib/l")]

INSTALL_MIXED = {
    "doc": "The queue has app/a one-shot and app/b normally: one plan, emerged "
           "one-shot first, then app/b (which goes in the world file)",
    "repo": INSTALL_PKGS, "installed": [], "world": [],
    "request": {"targets": ["--oneshot", "app/a", "app/b"], "world": ["app/b"]},
}

INSTALL_ONESHOT = {
    "doc": "The queue has only one-shot installs: one emerge",
    "repo": INSTALL_PKGS, "installed": [], "world": [],
    "request": {"targets": ["--oneshot", "app/a", "app/b"]},
}


INSTALL_ALL_WORLD = {
    "doc": "An unfinished install where only what was asked for is left: "
           "one-shot in the queue, but all of it for the world file",
    "repo": INSTALL_PKGS, "installed": [inst("lib/l-1")], "world": [],
    "request": {"targets": ["--oneshot", "=app/b-1"], "world": ["=app/b-1"]},
}


DEPCLEAN = {
    "doc": "Up to date, with a library nothing needs any more (lib/old) "
           "and one an installed package still needs (lib/l)",
    "repo": [pkg("app/a-1", rdepend="lib/l"), pkg("lib/l-1"), pkg("lib/old-1")],
    "installed": [inst("app/a-1", rdepend="lib/l"), inst("lib/l-1"), inst("lib/old-1")],
    "world": ["app/a"],
    "depclean": True,
}


LICENSED = {
    "doc": "app/viewer-2 needs lib/codec-2, whose licence (EULA) isn't accepted",
    "repo": [pkg("app/viewer-1", rdepend="lib/codec"),
             pkg("app/viewer-2", rdepend=">=lib/codec-2"),
             pkg("lib/codec-1"), pkg("lib/codec-2", license="EULA")],
    "installed": [inst("app/viewer-1", rdepend="lib/codec"), inst("lib/codec-1")],
    "world": ["app/viewer"],
    "accept_license": "MIT",
}

MASKED = {
    "doc": "app/player-2 needs lib/dec-2, which the profile masks",
    "repo": [pkg("app/player-1", rdepend="lib/dec"),
             pkg("app/player-2", rdepend=">=lib/dec-2"),
             pkg("lib/dec-1"), pkg("lib/dec-2")],
    "installed": [inst("app/player-1", rdepend="lib/dec"), inst("lib/dec-1")],
    "world": ["app/player"],
    "masked": ["=lib/dec-2"],
}


def with_choices(scenario, doc, **choices):
    return dict(scenario, doc=doc, choices=choices)


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
    "hard-blocker": HARD_BLOCKER,
    "everything": combine(CYCLES, OLD_DEP, SUBSLOT, FIXES, BLOCKER),
    "held-newer": HELD_NEWER,
    "held-stuck": HELD_STUCK,
    "held-stuck-keep": with_choices(
        HELD_STUCK, "held-stuck, and the user chose to keep lib/v and app/vnew",
        exclude=["lib/v", "app/vnew"]),
    "held-stuck-remove": with_choices(
        HELD_STUCK, "held-stuck, and the user chose to remove app/stuck",
        accept=["remove:app/stuck"]),
    "fixes-rejected": with_choices(
        FIXES, "fixes, but the user rejected the ~amd64 keyword",
        reject_fixes=["=lib/k-3 ~amd64"]),
    "blocker-declined": with_choices(
        BLOCKER, "blocker, but the user wants to keep app/oldtool",
        decline=["replace:app/oldtool"]),
    "licensed": LICENSED,
    "masked": MASKED,
    "install-mixed": INSTALL_MIXED,
    "install-oneshot": INSTALL_ONESHOT,
    "install-all-world": INSTALL_ALL_WORLD,
    "deselected": with_choices(
        CYCLES, "cycles, with media/q unticked", exclude=["media/q"]),
    "depclean": DEPCLEAN,
    "depclean-chosen": with_choices(
        DEPCLEAN, "depclean, and the user ticked lib/old", accept=["depclean:lib/old"]),
}


def expect(status, merge=(), held=(), mine=(), removed=(), unmerge=(), steps=None,
           unneeded=None):
    """What a scenario's plan should come to: its status, the versions it
    merges, the packages held back, the packages held by the user's
    choices, what it removes (kind:cpv) and what it unmerges outright;
    steps, if given: each stage's emerge commands, root options left out"""
    wanted = {"status": status, "merge": sorted(merge), "held": sorted(held),
              "mine": sorted(mine), "removed": sorted(removed), "unmerge": sorted(unmerge)}
    if steps is not None:
        wanted["steps"] = steps
    if unneeded is not None:
        # packages nothing needs, "(removing)" after the ones ticked
        wanted["unneeded"] = unneeded
    return wanted


EXPECTED = {
    "cycles": expect("needs-approval", ["lib/x-2", "lib/y-2", "media/p-2", "media/q-2"]),
    "old-dep": expect("needs-approval", ["app/newthing-2", "lib/z-2"],
                      removed=["orphan:app-old/legacy-1"]),
    "subslot": expect("needs-approval", ["app/cons-1", "lib/abi-2.0"],
                      removed=["orphan:app/frozen-1"]),
    "fixes": expect("needs-approval", ["app/tool-2", "lib/feat-1", "lib/k-3"]),
    "blocker": expect("needs-approval", ["app/conflict-2"],
                      removed=["replaced:app/oldtool-1"]),
    # portage won't replace a hard-blocked package itself: it is unmerged first
    "hard-blocker": expect("needs-approval", ["app/strict-2"],
                           removed=["replaced:app/legacytool-1"],
                           unmerge=["app/legacytool"]),
    "everything": expect(
        "needs-approval",
        ["app/conflict-2", "app/cons-1", "app/newthing-2", "app/tool-2", "lib/abi-2.0",
         "lib/feat-1", "lib/k-3", "lib/x-2", "lib/y-2", "lib/z-2", "media/p-2", "media/q-2"],
        removed=["orphan:app-old/legacy-1", "orphan:app/frozen-1",
                 "replaced:app/oldtool-1"]),
    "held-newer": expect("needs-approval", ["app/user-2", "app/wnew-2", "lib/w-2"]),
    "held-stuck": expect("needs-help", held=["app/vnew-2", "lib/v-2"]),
    "licensed": expect("needs-approval", ["app/viewer-2", "lib/codec-2"]),
    "masked": expect("needs-approval", ["app/player-2", "lib/dec-2"]),
    "held-stuck-keep": expect("up-to-date", mine=["app/vnew", "lib/v"]),
    "held-stuck-remove": expect("needs-approval", ["app/vnew-2", "lib/v-2"],
                                removed=["chosen:app/stuck-1"]),
    "fixes-rejected": expect("up-to-date", mine=["app/tool"]),
    "blocker-declined": expect("up-to-date", mine=["app/conflict"]),
    # media/p-2 needs media/q-2, so unticking media/q holds media/p too
    "install-mixed": expect("ready", ["app/a-1", "app/b-1", "lib/l-1"], steps=[
        ["emerge --keep-going --oneshot app/a"],
        ["emerge --keep-going app/b"],
        ["emerge --keep-going --oneshot @preserved-rebuild"]]),
    "install-oneshot": expect("ready", ["app/a-1", "app/b-1", "lib/l-1"], steps=[
        ["emerge --keep-going --oneshot app/a app/b"],
        ["emerge --keep-going --oneshot @preserved-rebuild"]]),
    "install-all-world": expect("ready", ["app/b-1"], steps=[
        ["emerge --keep-going =app/b-1"],
        ["emerge --keep-going --oneshot @preserved-rebuild"]]),
    "deselected": expect("needs-approval", ["lib/x-2", "lib/y-2"], mine=["media/p", "media/q"]),
    "depclean": expect("up-to-date", unneeded=["lib/old-1"], steps=[]),
    "depclean-chosen": expect("ready", unneeded=["lib/old-1 (removing)"], steps=[
        ["emerge --depclean =lib/old-1"],
        ["emerge --keep-going --oneshot @preserved-rebuild"]]),
}


def outcome(plan):
    """A plan, in EXPECTED's terms"""
    return expect(plan["status"],
                  [m["cpv"] for m in plan["merge_list"]],
                  [h["cpv"] for h in plan["held_back"]],
                  [h["cp"] for h in plan["held_by_you"]],
                  ["%s:%s" % (r["kind"], r["cpv"]) for r in plan.get("removed") or []],
                  [c[-1] for st in plan["stages"] for c in st["commands"] if "--unmerge" in c],
                  [[" ".join(a for a in c if not a.startswith(("--root=", "--sysroot=")))
                    for c in st["commands"]] for st in plan["stages"]],
                  ["%s%s" % (p["cpv"], " (removing)" if p["remove"] else "")
                   for p in (plan.get("unneeded") or {}).get("packages") or []])


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
              'LICENSE="%(license)s"\nSLOT="%(slot)s"\nKEYWORDS="%(keywords)s"\n'
              'IUSE="%(iuse)s"\nDEPEND="%(depend)s"\nRDEPEND="%(rdepend)s"\n'
              'S="${WORKDIR}"\n' % p)

    etc = os.path.join(config, "etc", "portage")
    write(os.path.join(etc, "make.conf"),
          'ACCEPT_LICENSE="%s"\nFEATURES="-news"\n'
          'PORTAGE_TMPDIR="%s"\nDISTDIR="%s"\nPKGDIR="%s"\n'
          % (scenario.get("accept_license", "*"), os.path.join(work, "tmp"),
             os.path.join(work, "distfiles"), os.path.join(work, "packages")))
    if scenario.get("masked"):
        write(os.path.join(repo, "profiles", "package.mask"),
              "".join(a + "\n" for a in scenario["masked"]))
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


def run(name, keep=False, quiet=False):
    scenario = SCENARIOS[name]
    work = tempfile.mkdtemp(prefix="porthole-scenario-%s-" % name)
    try:
        config, root = build(work, scenario)
        out = os.path.join(work, "plan.json")
        env = dict(os.environ, PYTHONPATH=SRC)
        choices = []
        system = ["--root=" + root, "--sysroot=" + root]
        if scenario.get("request"):
            # an install from the queue, as Porthole asks for it
            request = dict(scenario["request"], choices={}, requested=[])
            request["targets"] = request["targets"] + system
            write(os.path.join(work, "request.json"), json.dumps(request))
            choices = ["--request", os.path.join(work, "request.json")]
        if scenario.get("choices"):
            write(os.path.join(work, "choices.json"), json.dumps(scenario["choices"]))
            choices = ["--choices", os.path.join(work, "choices.json")]
        proc = subprocess.run(
            [sys.executable, "-m", "porthole.updater.analyze", "--json", out] + choices
            + ["--config-root", config] + (["--depclean"] if scenario.get("depclean") else [])
            + ([] if scenario.get("request") else
               ["--", "--update", "--deep", "--newuse", "@world"] + system),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=1800)
        if not quiet:
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
    quiet = "-q" in argv
    names = [a for a in argv if not a.startswith("-")] or list(SCENARIOS)
    failed = []
    for name in names:
        got, wanted = outcome(run(name, keep, quiet)), EXPECTED[name]
        wrong = {k: got[k] for k in wanted if got[k] != wanted[k]}
        print("%s %s" % ("ok  " if not wrong else "FAIL", name), flush=True)
        for key, value in wrong.items():
            print("     %s: got %s, expected %s" % (key, value, wanted[key]))
        if wrong:
            failed.append(name)
    print("%d of %d scenarios as expected" % (len(names) - len(failed), len(names)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
