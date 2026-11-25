"""
    Kernels merged into a system from outside are installed later, inside it
    (plugins/crossdev/build.py kernel_block, root.py step_kernel): a fake
    kernel package merged with real portage into a scratch ROOT, as the
    user. The merge must only note it; the package's saved environment,
    run as emerge --config runs it inside the system (ROOT=/), must do the
    real install.

        python3 tests/updater/kernel_test.py
"""

import bz2
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from porthole.plugins.crossdev import build  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, (": %s" % detail) if detail and not ok
                       else ""))
    failed += not ok


ECLASS = """dist-kernel_install_kernel() {
\tmkdir -p "${EROOT}/boot" && echo "real install ${PF}" > "${EROOT}/boot/installed"
}
"""
EBUILD = """EAPI=8
inherit fakedist
SLOT=0
KEYWORDS="~amd64 amd64 ~arm64 arm64"
S=${WORKDIR}
src_install() { insinto /usr/share/fake; newins - marker <<< x; }
pkg_postinst() { dist-kernel_install_kernel; }
pkg_config() { pkg_postinst; }
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def main():
    import portage
    gentoo = portage.settings.repositories["gentoo"].location
    profile = os.path.realpath(os.path.join(portage.settings["PORTAGE_CONFIGROOT"],
                                            "etc/portage/make.profile"))
    with tempfile.TemporaryDirectory() as scratch:
        repo, cfg, root = (os.path.join(scratch, n) for n in ("repo", "cfg", "root"))
        write(os.path.join(repo, "profiles/repo_name"), "kt\n")
        write(os.path.join(repo, "metadata/layout.conf"), "masters = gentoo\nthin-manifests = true\n")
        write(os.path.join(repo, "eclass/fakedist.eclass"), ECLASS)
        ebuild = os.path.join(repo, "sys-kernel/fake-kernel/fake-kernel-1.ebuild")
        write(ebuild, EBUILD)
        etc = os.path.join(cfg, "etc/portage")
        write(os.path.join(etc, "repos.conf/r.conf"),
              "[DEFAULT]\nmain-repo = gentoo\n[gentoo]\nlocation = %s\n[kt]\nlocation = %s\n"
              % (gentoo, repo))
        os.symlink(profile, os.path.join(etc, "make.profile"))
        write(os.path.join(etc, "make.conf"),
              'FEATURES="-sandbox -usersandbox -userpriv -userfetch -pid-sandbox '
              '-network-sandbox -ipc-sandbox -mount-sandbox -preserve-libs -news"\n'
              'PORTAGE_TMPDIR="%s"\n' % os.path.join(scratch, "tmp"))
        write(os.path.join(etc, "bashrc"), "\n".join(build.kernel_block()) + "\n")
        os.makedirs(root)
        import grp
        import pwd
        env = dict(os.environ, PORTAGE_CONFIGROOT=cfg, ROOT=root,
                   PORTAGE_USERNAME=pwd.getpwuid(os.getuid()).pw_name,
                   PORTAGE_GRPNAME=grp.getgrgid(os.getgid()).gr_name)
        for phases in (["manifest"], ["clean", "merge"]):
            result = subprocess.run(["ebuild", ebuild] + phases, env=env,
                                    capture_output=True, text=True)
        check("fake kernel merged", result.returncode == 0, result.stderr[-500:])
        check("merge noted the kernel", build.kernels_waiting(root) == ["sys-kernel/fake-kernel-1"],
              build.kernels_waiting(root))
        check("merge wrote no /boot", not os.path.exists(os.path.join(root, "boot")))
        with bz2.open(os.path.join(root, "var/db/pkg/sys-kernel/fake-kernel-1/environment.bz2"),
                      "rt") as f:
            saved = f.read()
        inside = os.path.join(scratch, "inside")
        script = os.path.join(scratch, "env.sh")
        write(script, saved)
        result = subprocess.run(["bash", "-c", 'source "$1" 2>/dev/null; ROOT=/ EROOT="$2"; '
                                 'dist-kernel_install_kernel', "_", script, inside],
                                capture_output=True, text=True)
        installed = os.path.join(inside, "boot/installed")
        check("in the system (ROOT=/) the real install runs", os.path.exists(installed),
              result.stderr[-300:])
        build.kernel_installed(root, "sys-kernel/fake-kernel-1")
        check("taken off the list", build.kernels_waiting(root) == [])


main()
print("%d failed" % failed)
sys.exit(1 if failed else 0)
