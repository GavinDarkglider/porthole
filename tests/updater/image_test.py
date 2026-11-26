"""
    Images of a built system (plugins/crossdev/image.py): recipes a
    system's packages install, what they still need, links resolved inside
    the system, and an image made into a scratch folder, with export and
    mksquashfs stood in for (the real ones need root and squashfs-tools).

        python3 tests/updater/image_test.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from porthole.plugins.crossdev import export as ex, image as im  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, (": %s" % detail) if detail and not ok
                       else ""))
    failed += not ok


RECIPE = """# a board's live image
[image]
title = Live SD card
about = Copy it to the card.
needs = sys-kernel/board-kernel[live] sys-boot/board-boot[live,-debug]
squashfs = live/rootfs.squashfs
compression = xz
max_file_size = 1000

[files]
live/kernel = /boot/uImage
bootloader/board.ini = /usr/share/board/board.ini
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Target:
    def __init__(self, root):
        self.root = root
        self.name = "board"
        self.title = "board"


def main():
    with tempfile.TemporaryDirectory() as scratch:
        root = os.path.join(scratch, "root")
        write(os.path.join(root, im.RECIPES_DIR, "board-live.conf"), RECIPE)
        write(os.path.join(root, im.RECIPES_DIR, "broken.conf"), "[image]\nneeds = not-an-atom\n")
        write(os.path.join(root, "var/db/pkg/sys-kernel/board-kernel-1.0/USE"), "arm64 strip\n")
        write(os.path.join(root, "var/db/pkg/sys-boot/board-boot-2/USE"), "live debug\n")
        # a package whose name starts the same isn't the one asked for
        write(os.path.join(root, "var/db/pkg/sys-kernel/board-kernel-sources-1/USE"), "live\n")
        write(os.path.join(root, "boot/uImage-1.0"), "kernel")
        # an absolute link means the system's /, never this computer's
        os.symlink("/boot/uImage-1.0", os.path.join(root, "boot/uImage"))

        found, broken = im.recipes(root)
        check("the recipe is read", list(found) == ["board-live"], found)
        check("a broken recipe is reported, not used",
              len(broken) == 1 and "not-an-atom" in broken[0][1], broken)
        recipe = found["board-live"]
        check("its needs", recipe.needs == [("sys-kernel/board-kernel", ["live"]),
                                            ("sys-boot/board-boot", ["live", "-debug"])],
              recipe.needs)
        check("its files", recipe.files == [("live/kernel", "/boot/uImage"),
                                            ("bootloader/board.ini", "/usr/share/board/board.ini")],
              recipe.files)

        missing = im.missing_needs(root, recipe)
        check("a flag that's off, and one that should be", missing == [
            "sys-kernel/board-kernel is built without USE=live",
            "sys-boot/board-boot is built with USE=debug"], missing)
        check("links followed inside the system",
              im.in_root(root, "/boot/uImage") == os.path.join(root, "boot/uImage-1.0"))
        check("a file it lacks", im.missing_sources(root, recipe) ==
              ["/usr/share/board/board.ini"], im.missing_sources(root, recipe))

        for text, why in (("[image]\nsquashfs = ../x\n", "outside the image"),
                          ("[image]\nsquashfs = a\ncompression = rar\n", "compression"),
                          ("[image]\ntitle = nothing\n", "makes nothing"),
                          ("[image]\n[files]\na = relative\n", "a path in the system")):
            path = os.path.join(scratch, "bad.conf")
            write(path, text)
            try:
                im.read_recipe(path)
                check("refused: %s" % why, False, "read")
            except im.ImageError:
                check("refused: %s" % why, True)

        target = Target(root)
        images = os.path.join(scratch, "images")
        try:
            im.make(target, recipe, {}, lambda argv: True, log=lambda m: None,
                    images_dir=images)
            check("not made while it lacks something", False, "made")
        except im.ImageError as e:
            check("not made while it lacks something", "USE=live" in str(e), e)

        write(os.path.join(root, "var/db/pkg/sys-kernel/board-kernel-1.0/USE"), "live\n")
        write(os.path.join(root, "var/db/pkg/sys-boot/board-boot-2/USE"), "live\n")
        write(os.path.join(root, "usr/share/board/board.ini"), "[board]\n")
        ran = []

        def export(target_, dest, repos, log=print):
            write(os.path.join(dest, "etc/os-release"), "ID=gentoo\n")
            return dest

        def mksquashfs(argv):
            ran.append(argv)
            write(argv[2], "squashfs")
            return True
        real = ex.export, im.squashfs_tool
        ex.export, im.squashfs_tool = export, lambda compression: None
        try:
            card = im.make(target, recipe, {}, mksquashfs, log=lambda m: None,
                           images_dir=images)
        finally:
            ex.export, im.squashfs_tool = real
        made = sorted(os.path.relpath(os.path.join(f, n), card)
                      for f, _d, ns in os.walk(card) for n in ns)
        check("the image's files", made == ["bootloader/board.ini", "live/kernel",
                                            "live/rootfs.squashfs"], made)
        check("the kernel copied from where its link points",
              open(os.path.join(card, "live/kernel")).read() == "kernel")
        check("mksquashfs with the recipe's compression, leftovers left out",
              ran and ran[0][ran[0].index("-comp") + 1] == "xz" and "var/tmp/*" in ran[0],
              ran)
        check("the copy for the squashfs is gone again",
              not os.path.exists(os.path.join(images, "board", "board-live", "system")))

        write(os.path.join(root, "usr/share/board/board.ini"), "x" * 2000)
        ex.export, im.squashfs_tool = export, lambda compression: None
        try:
            im.make(target, recipe, {}, mksquashfs, log=lambda m: None, images_dir=images)
            check("a file too big for the card stops it", False, "made")
        except im.ImageError as e:
            check("a file too big for the card stops it", "board.ini" in str(e), e)
        finally:
            ex.export, im.squashfs_tool = real
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
