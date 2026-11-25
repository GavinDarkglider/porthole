"""
    Services started at boot in a built system (plugins/crossdev/firstboot.py):
    the choices checked, suggestions read from the packages' install notes,
    runlevel links (OpenRC) and .wants links (systemd) written.

        python3 tests/updater/services_test.py
"""

import bz2
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from porthole.plugins.crossdev import firstboot as fb  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, (": %s" % detail) if detail and not ok
                       else ""))
    failed += not ok


def make_root(folder, init):
    root = os.path.join(folder, "root")
    portage = os.path.join(root, "etc/portage")
    os.makedirs(portage)
    profile = os.path.join(folder, "profiles", "default/linux/arm64/23.0" +
                           ("/systemd" if init == "systemd" else ""))
    os.makedirs(profile)
    os.symlink(profile, os.path.join(portage, "make.profile"))
    return types.SimpleNamespace(name="t", root=root, portage_dir=portage)


def script(root, name):
    path = os.path.join(root, "etc/init.d", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/sbin/openrc-run\n")
    os.chmod(path, 0o755)


def package(root, cpv, text):
    pkg = os.path.join(root, "var/db/pkg", cpv)
    os.makedirs(pkg)
    with bz2.open(os.path.join(pkg, "environment.bz2"), "wt") as f:
        f.write(text)


def test_check():
    out = fb.check({"services": {"joycond": "default", "cronie": "off"}})
    check("choices kept", out["services"] == {"joycond": "default", "cronie": "off"})
    for bad in ({"../x": "default"}, {"x": "later"}, ["x"]):
        try:
            fb.check({"services": bad})
            check("bad choice refused: %r" % (bad,), False)
        except fb.FirstBootError:
            check("bad choice refused: %r" % (bad,), True)


def test_openrc():
    with tempfile.TemporaryDirectory() as folder:
        target = make_root(folder, "openrc")
        root = target.root
        for name in ("joycond", "cronie", "metalog", "udev", "functions.sh"):
            script(root, name)
        os.makedirs(os.path.join(root, "etc/runlevels/sysinit"))
        os.symlink("/etc/init.d/udev", os.path.join(root, "etc/runlevels/sysinit/udev"))
        os.makedirs(os.path.join(root, "etc/runlevels/boot"))
        os.symlink("/etc/init.d/metalog", os.path.join(root, "etc/runlevels/boot/metalog"))
        package(root, "games-util/joycond-9999",
                'pkg_postinst () {\n  elog "  OpenRC:  rc-update add ${PN} default"\n}\n')
        check("services the system has", fb.available_services(target) ==
              ["cronie", "joycond", "metalog", "udev"], fb.available_services(target))
        rows = {r["name"]: r for r in fb.services_view(target, {"network": "none"})}
        check("package's install note suggests joycond",
              rows["joycond"]["suggested"] == "default" and rows["joycond"]["level"] is None
              and "games-util/joycond-9999" in rows["joycond"]["source"], rows.get("joycond"))
        check("usual cron daemon suggested", rows["cronie"]["suggested"] == "default",
              rows.get("cronie"))
        check("already started: no suggestion", rows["udev"]["level"] == "sysinit"
              and rows["udev"]["suggested"] is None, rows.get("udev"))
        log = []
        fb.apply_services(target, {"services": {"joycond": "default", "metalog": "default",
                                                "udev": "off"}}, log.append)
        runlevels = os.path.join(root, "etc/runlevels")
        check("added to default", os.readlink(os.path.join(runlevels, "default/joycond"))
              == "/etc/init.d/joycond")
        check("moved from boot to default", os.path.lexists(
            os.path.join(runlevels, "default/metalog")) and not os.path.lexists(
            os.path.join(runlevels, "boot/metalog")))
        check("not started any more", not os.path.lexists(os.path.join(runlevels,
                                                                        "sysinit/udev")))
        check("started_services reads it back", fb.started_services(target) ==
              {"joycond": "default", "metalog": "default"}, fb.started_services(target))


def test_systemd():
    with tempfile.TemporaryDirectory() as folder:
        target = make_root(folder, "systemd")
        units = os.path.join(target.root, "usr/lib/systemd/system")
        os.makedirs(units)
        with open(os.path.join(units, "joycond.service"), "w") as f:
            f.write("[Install]\nWantedBy=graphical.target\n")
        with open(os.path.join(units, "cronie.service"), "w") as f:
            f.write("[Service]\nExecStart=/usr/sbin/crond\n")
        fb.apply_services(target, {"services": {"joycond": "default", "cronie": "boot"}})
        wants = os.path.join(target.root, "etc/systemd/system")
        check("enabled where its unit asks", os.readlink(os.path.join(
            wants, "graphical.target.wants/joycond.service"))
            == "/usr/lib/systemd/system/joycond.service")
        check("multi-user.target when it doesn't say", os.path.lexists(
            os.path.join(wants, "multi-user.target.wants/cronie.service")))
        fb.apply_services(target, {"services": {"joycond": "off"}})
        check("disabled", not os.path.lexists(os.path.join(
            wants, "graphical.target.wants/joycond.service")))


test_check()
test_openrc()
test_systemd()
print("%d failed" % failed)
sys.exit(1 if failed else 0)
