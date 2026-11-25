"""
    Configuration file updates (updater/configupdates.py), in a scratch
    system: finding ._cfg files, sorting them out, merging, settling them
    and the copy kept for the next three-way merge.

        python3 tests/updater/config_updates_test.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from porthole.updater import configupdates as cu  # noqa: E402
from porthole.updater.targets import Target  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, (": %r" % (detail,)) if detail and not ok
                       else ""))
    failed += not ok


def write(root, name, text, mode=0o644):
    path = os.path.join(root, name.lstrip("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def read(root, name):
    with open(os.path.join(root, name.lstrip("/"))) as f:
        return f.read()


def main():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as archive:
        target = Target("test", "Test system", root, root)
        cu.protect_lists = lambda t: (["/etc", "/usr/share/config"], ["/etc/env.d"])
        files = {
            # name: (yours, new version)
            "/etc/same.conf": ("a = 1\n", "a = 1\n"),
            "/etc/comments.conf": ("# old note\na = 1\n", "# new note\n\na = 1\n"),
            "/etc/conf.d/svc": ('# settings\nOPTS="-v"\nPORT=1\n',
                                '# settings, documented\nOPTS="-q"\nPORT=1\n# new\nNEW=yes\n'),
            "/etc/other.txt": ("line one\nmine\n", "line one\ntheirs\n"),
            "/etc/env.d/99masked": ("X=1\n", "X=2\n"),
            "/etc/.hidden/x.conf": ("a\n", "b\n"),
        }
        for name, (yours, new) in files.items():
            write(root, name, yours)
            folder, base = os.path.split(name)
            write(root, os.path.join(folder, "._cfg0000_" + base), new)
        write(root, "/etc/._cfg0001_other.txt", "line one\ntheirs, newer\n")

        found = {u.name: u for u in cu.pending(target)}
        check("found the waiting files, not masked or hidden ones",
              sorted(found) == ["/etc/comments.conf", "/etc/conf.d/svc", "/etc/other.txt",
                                "/etc/same.conf"], sorted(found))
        check("the newest of several versions",
              found["/etc/other.txt"].newest.endswith("._cfg0001_other.txt"))
        kinds = {n: cu.classify(target, u, archive)["kind"] for n, u in found.items()}
        check("sorted out", kinds == {"/etc/same.conf": "same", "/etc/comments.conf": "comments",
                                      "/etc/conf.d/svc": "settings", "/etc/other.txt": "differs"},
              kinds)

        settings = cu.classify(target, found["/etc/conf.d/svc"], archive)
        check("settings: your value, the new setting, the new comments",
              'OPTS="-v"' in settings["result"] and "NEW=yes" in settings["result"]
              and "documented" in settings["result"], settings["result"])

        done, left = cu.auto(target, archive, log=lambda m: None)
        check("settled by themselves", sorted(done) == ["/etc/comments.conf", "/etc/same.conf"],
              done)
        check("left for you", sorted(left) == ["/etc/conf.d/svc", "/etc/other.txt"], left)
        check("comments only: the new version taken", read(root, "/etc/comments.conf")
              == "# new note\n\na = 1\n")
        check("their ._cfg files gone", not os.path.exists(
            os.path.join(root, "etc/._cfg0000_same.conf")))
        check("the package's version kept for next time",
              os.path.exists(cu.archive_path(target, "/etc/same.conf", archive)))

        cu.apply(target, "/etc/other.txt", "keep", archive_dir=archive, log=lambda m: None)
        check("keep: yours stays, both versions gone", read(root, "/etc/other.txt")
              == "line one\nmine\n" and not any(
                  n.startswith("._cfg") and n.endswith("other.txt")
                  for n in os.listdir(os.path.join(root, "etc"))))
        cu.apply(target, "/etc/conf.d/svc", "merged", settings["result"], archive,
                 log=lambda m: None)
        check("merged result saved", read(root, "/etc/conf.d/svc") == settings["result"])

        # the next update of other.txt: three-way, with the version kept
        write(root, "/etc/._cfg0000_other.txt", "line zero\nline one\ntheirs, newer\n")
        update = {u.name: u for u in cu.pending(target)}["/etc/other.txt"]
        merged = cu.classify(target, update, archive)
        check("three-way: both changes, no conflict", merged["kind"] == "merged"
              and merged["result"] == "line zero\nline one\nmine\n", merged)
        write(root, "/etc/._cfg0000_other.txt", "line one\nsomething else\n")
        update = {u.name: u for u in cu.pending(target)}["/etc/other.txt"]
        conflict = cu.classify(target, update, archive)
        check("three-way: same line changed by both is a conflict",
              conflict["kind"] == "conflict" and "<<<<<<<" in conflict["result"], conflict)

        try:
            cu.apply(target, "/etc/passwd", "new", archive_dir=archive)
            check("a file with nothing waiting is refused", False)
        except cu.ConfigUpdateError:
            check("a file with nothing waiting is refused", True)

        secret = write(root, "/etc/secret.conf", "key=1\n", 0o600)
        write(root, "/etc/._cfg0000_secret.conf", "key=2\n", 0o600)
        cu.apply(target, "/etc/secret.conf", "new", archive_dir=archive, log=lambda m: None)
        mode = os.stat(cu.archive_path(target, "/etc/secret.conf", archive)).st_mode & 0o777
        check("a private file's copy stays private", mode == 0o600, oct(mode))
        check("new version taken", read(root, "/etc/secret.conf") == "key=2\n"
              and os.stat(secret).st_mode & 0o777 == 0o600)


main()
print("%d failed" % failed)
sys.exit(1 if failed else 0)
