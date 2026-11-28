"""
    A system's binary packages tested against the glibc it gets
    (plugins/crossdev/health.py newer_libc_binpkgs): packages whose programs
    or libraries need a newer glibc are found, the rest kept, each package
    file read once.

        python3 tests/updater/binpkgs_libc_test.py
"""

import io
import os
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from porthole.plugins.crossdev import conf, health as hc  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, (": %s" % detail) if detail and not ok
                       else ""))
    failed += not ok


def elf(*versions):
    """An ELF-looking file naming glibc versions as its dynamic strings do"""
    return b"\x7fELF" + b"\x00" * 60 + b"".join(v.encode() + b"\x00" for v in versions)


def gpkg(path, files):
    """A gpkg: an outer tar holding <name>/image.tar.xz of files"""
    image = io.BytesIO()
    with tarfile.open(fileobj=image, mode="w:xz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo("image/" + name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, "w") as outer:
        info = tarfile.TarInfo("pkg/image.tar.xz")
        info.size = len(image.getvalue())
        outer.addfile(info, io.BytesIO(image.getvalue()))


class Target:
    name = "board"


def main():
    with tempfile.TemporaryDirectory() as scratch:
        pkgdir = os.path.join(scratch, "binpkgs")
        cache = os.path.join(scratch, "cache")
        gpkg(os.path.join(pkgdir, "dev-db/sqlite/sqlite-3-1.gpkg.tar"),
             {"usr/lib64/libsqlite3.so.0": elf("GLIBC_2.17", "GLIBC_2.44")})
        gpkg(os.path.join(pkgdir, "sys-libs/glibc/glibc-2.44-r5-1.gpkg.tar"),
             {"lib64/libm.so.6": elf("GLIBC_2.17", "GLIBC_2.43", "GLIBC_2.44")})
        gpkg(os.path.join(pkgdir, "sys-libs/glibc/glibc-2.43-r4-1.gpkg.tar"),
             {"lib64/libm.so.6": elf("GLIBC_2.17", "GLIBC_2.43")})
        gpkg(os.path.join(pkgdir, "app-misc/old/old-1-1.gpkg.tar"),
             {"usr/bin/old": elf("GLIBC_2.17", "GLIBC_2.34", "GLIBC_PRIVATE")})
        # text naming a version isn't a program needing it
        gpkg(os.path.join(pkgdir, "app-doc/notes/notes-1-1.gpkg.tar"),
             {"usr/share/doc/notes": b"needs GLIBC_2.99\x00 " * 10})
        real = conf.effective, conf.read
        conf.effective, conf.read = lambda values, name: pkgdir, lambda target: {}
        logged = []
        try:
            _dir, newer = hc.newer_libc_binpkgs(Target(), (2, 43), cache, logged.append)
            check("packages needing a newer glibc found", sorted(newer) == [
                ("dev-db/sqlite/sqlite-3-1.gpkg.tar", "2.44"),
                ("sys-libs/glibc/glibc-2.44-r5-1.gpkg.tar", "2.44")], newer)
            check("the first test says it reads them", logged and "5 binary packages" in
                  logged[0], logged)
            logged.clear()
            _dir, newer = hc.newer_libc_binpkgs(Target(), (2, 44), cache, logged.append)
            check("with glibc 2.44 nothing is too new", newer == [], newer)
            check("a package already tested isn't read again", logged == [], logged)
            _dir, newer = hc.newer_libc_binpkgs(Target(), (2, 33), cache)
            check("an older glibc finds the 2.34 program too",
                  ("app-misc/old/old-1-1.gpkg.tar", "2.34") in newer, newer)
        finally:
            conf.effective, conf.read = real
        check("versions read", hc.libc_version("2.43-r4") == (2, 43) and
              hc.libc_version("2.44.9999") == (2, 44) and hc.libc_version("") == ())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
