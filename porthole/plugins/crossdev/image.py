"""
    Porthole crossdev plugin: a built system made into an image to boot

    What an image is depends on the computer, so the system says it: a
    recipe its board's package installs in /usr/share/porthole/images
    (<id>.conf). Porthole only runs the generic steps a recipe names: the
    system copied out for its computer (export.py), made into a squashfs,
    the files the bootloader loads copied next to it, and every file
    checked against the size the card's filesystem allows. The result is a
    folder whose contents go onto the card.

    [image]
    title = Live SD card
    about = What it is and where its files go.
    # packages it needs in the system, with the USE flags that matter
    needs = sys-kernel/some-kernel[live] sys-boot/some-bootfiles[live]
    # the system as a squashfs, at this place in the image
    squashfs = boot/rootfs.squashfs
    compression = xz
    # the largest a file may be (FAT32: 4 GiB less a byte)
    max_file_size = 4294967295

    [files]
    # place in the image = file in the system (links followed)
    boot/kernel = /boot/vmlinuz

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import configparser
import glob
import os
import re
import shutil
import subprocess

RECIPES_DIR = "usr/share/porthole/images"
IMAGES_DIR = "/var/lib/porthole/images"
COMPRESSIONS = ("xz", "zstd", "gzip", "lz4", "lzo", "lzma")
# what never goes into an image: leftovers of building, downloads
SQUASHFS_EXCLUDES = ["tmp/*", "var/tmp/*", "var/cache/distfiles/*"]
_NEED = re.compile(r"^([A-Za-z0-9+_.-]+/[A-Za-z0-9+_-]+)(?:\[([^\]]*)\])?$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ImageError(Exception):
    pass


class Recipe:
    def __init__(self, id_, path, title, about, needs, squashfs, compression,
                 max_file_size, files):
        self.id = id_
        self.path = path
        self.title = title
        self.about = about
        self.needs = needs
        self.squashfs = squashfs
        self.compression = compression
        self.max_file_size = max_file_size
        self.files = files

    def __repr__(self):
        return "<Recipe %s>" % self.id


def _place(text, what):
    """A place inside the image: relative, without .. or empty parts"""
    parts = text.strip().strip("/").split("/")
    if not text.strip() or any(p in ("", ".", "..") for p in parts):
        raise ImageError("%s isn't a place in the image: %r" % (what, text))
    return "/".join(parts)


def read_recipe(path):
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.optionxform = str
    try:
        with open(path) as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as e:
        raise ImageError("%s can't be read: %s" % (path, e))
    if not parser.has_section("image"):
        raise ImageError("%s has no [image] section" % path)
    image = parser["image"]
    id_ = os.path.basename(path)[:-len(".conf")]
    needs = []
    for word in image.get("needs", "").split():
        match = _NEED.match(word)
        if not match:
            raise ImageError("%s: %r isn't category/package[flags]" % (path, word))
        needs.append((match.group(1), [f for f in (match.group(2) or "").split(",") if f]))
    squashfs = image.get("squashfs")
    squashfs = _place(squashfs, "%s: squashfs" % path) if squashfs else None
    compression = image.get("compression", "xz").strip()
    if compression not in COMPRESSIONS:
        raise ImageError("%s: compression %r isn't one of %s" % (
            path, compression, ", ".join(COMPRESSIONS)))
    try:
        max_file_size = int(image.get("max_file_size", "0")) or None
    except ValueError:
        raise ImageError("%s: max_file_size isn't a number of bytes" % path)
    files = []
    if parser.has_section("files"):
        for place, source in parser["files"].items():
            source = source.strip()
            if not source.startswith("/"):
                raise ImageError("%s: %s comes from %r, not a path in the system" % (
                    path, place, source))
            files.append((_place(place, path), source))
    if not squashfs and not files:
        raise ImageError("%s makes nothing: no squashfs and no [files]" % path)
    return Recipe(id_, path, image.get("title", id_).strip(),
                  " ".join(image.get("about", "").split()), needs, squashfs, compression,
                  max_file_size, files)


def recipes(root):
    """{id: Recipe} the system's packages installed, and [(path, error)]
    for the ones that can't be used"""
    found, broken = {}, []
    for path in sorted(glob.glob(os.path.join(root, RECIPES_DIR, "*.conf"))):
        if not _SAFE_ID.match(os.path.basename(path)[:-len(".conf")]):
            continue
        try:
            recipe = read_recipe(path)
        except ImageError as e:
            broken.append((path, str(e)))
            continue
        found[recipe.id] = recipe
    return found, broken


def _installed_use(root, cp):
    """(version, set of enabled USE flags) of cp installed in root, or None"""
    category, package = cp.split("/", 1)
    for folder in sorted(glob.glob(os.path.join(root, "var/db/pkg", category, package + "-*"))):
        pf = os.path.basename(folder)
        if not re.match(re.escape(package) + r"-\d", pf):
            continue
        try:
            with open(os.path.join(folder, "USE")) as f:
                use = set(f.read().split())
        except OSError:
            use = set()
        return pf[len(package) + 1:], use
    return None


def missing_needs(root, recipe):
    """What the system lacks for the recipe, as sentences"""
    missing = []
    for cp, flags in recipe.needs:
        installed = _installed_use(root, cp)
        if installed is None:
            missing.append("%s isn't installed in it" % cp)
            continue
        off = [f for f in flags if not f.startswith("-") and f not in installed[1]]
        on = [f[1:] for f in flags if f.startswith("-") and f[1:] in installed[1]]
        if off:
            missing.append("%s is built without USE=%s" % (cp, " ".join(off)))
        if on:
            missing.append("%s is built with USE=%s" % (cp, " ".join(on)))
    return missing


def in_root(root, path):
    """path in the system as a path here: links followed inside the system
    (an absolute link means the system's /, not this computer's)"""
    parts = path.strip("/").split("/")
    done = root
    for hops in range(40):
        if not parts:
            return done
        part = parts.pop(0)
        here = os.path.join(done, part)
        if os.path.islink(here):
            target = os.readlink(here)
            if target.startswith("/"):
                done = root
            parts = [p for p in target.split("/") if p] + parts
            continue
        if part == "..":
            done = os.path.dirname(done) if done != root else root
        elif part not in ("", "."):
            done = here
    raise ImageError("%s: too many links" % path)


def missing_sources(root, recipe):
    """The recipe's files the system doesn't have"""
    return [source for _place_, source in recipe.files
            if not os.path.isfile(in_root(root, source))]


def squashfs_tool(compression):
    """None when mksquashfs is here and can compress that way, else why not"""
    tool = shutil.which("mksquashfs")
    if tool is None:
        return "mksquashfs isn't installed on this computer (sys-fs/squashfs-tools)"
    result = subprocess.run([tool, "-help"], capture_output=True, text=True)
    text = result.stdout + result.stderr
    # it lists the compressors it was built with
    if compression not in re.findall(r"^\s*(\w+)\b", text, re.M):
        return ("this computer's mksquashfs can't compress with %s (sys-fs/squashfs-tools "
                "needs the USE flag for it: lzma for xz)" % compression)
    return None


def output_dir(name, recipe_id, images_dir=None):
    return os.path.join(images_dir or IMAGES_DIR, name, recipe_id)


def _file_too_big(card, limit):
    too_big = []
    for folder, _dirs, names in os.walk(card):
        for n in names:
            path = os.path.join(folder, n)
            if os.path.getsize(path) > limit:
                too_big.append("%s (%d MiB)" % (os.path.relpath(path, card),
                                                os.path.getsize(path) >> 20))
    return too_big


def make(target, recipe, repos, run, log=print, images_dir=None):
    """The image of target from recipe, in <output_dir>/card; run(argv)
    runs a command with its output in the log and says whether it worked"""
    from porthole.plugins.crossdev import export as ex
    root = os.path.realpath(target.root)
    missing = missing_needs(root, recipe) + [
        "%s isn't in it" % s for s in missing_sources(root, recipe)]
    if missing:
        raise ImageError("%s can't be made from %s yet: %s" % (
            recipe.title, target.title, "; ".join(missing)))
    if recipe.squashfs:
        why = squashfs_tool(recipe.compression)
        if why:
            raise ImageError(why)
    out = output_dir(target.name, recipe.id, images_dir)
    card = os.path.join(out, "card")
    staging = os.path.join(out, "system")
    if os.path.isdir(card):
        shutil.rmtree(card)
    os.makedirs(card, mode=0o755)
    if recipe.squashfs:
        # the system as it is set up for its computer (export.py), not for
        # building here
        # a copy a stopped run left is only ever ours: made again
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        os.makedirs(staging, mode=0o755)
        log("== The system, set up for its computer, in %s" % staging)
        ex.export(target, staging, repos, log=log)
        image = os.path.join(card, recipe.squashfs)
        os.makedirs(os.path.dirname(image), mode=0o755, exist_ok=True)
        argv = ["mksquashfs", staging, image, "-noappend", "-comp", recipe.compression,
                "-no-progress", "-wildcards", "-e", ex.MARK] + SQUASHFS_EXCLUDES
        log("== The system as a squashfs: %s" % recipe.squashfs)
        if not run(argv):
            raise ImageError("mksquashfs didn't make the squashfs (above)")
        # the copy is only for making the squashfs: the space goes back
        shutil.rmtree(staging)
    for place, source in recipe.files:
        dest = os.path.join(card, place)
        os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
        shutil.copyfile(in_root(root, source), dest)
        os.chmod(dest, 0o644)
        log("%s <- %s" % (place, source))
    if recipe.max_file_size:
        too_big = _file_too_big(card, recipe.max_file_size)
        if too_big:
            raise ImageError("Too big for the card's filesystem (at most %d bytes a file): %s"
                             % (recipe.max_file_size, ", ".join(too_big)))
    total = sum(os.path.getsize(os.path.join(f, n)) for f, _d, ns in os.walk(card) for n in ns)
    log("%s of %s: %d MiB in %s" % (recipe.title, target.title, total >> 20, card))
    return card
