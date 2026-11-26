#!/usr/bin/env python3
"""
    system.py: reading make.conf as its author laid it out, and the
    findings worked out from it.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from porthole.updater import system  # noqa: E402

failed = 0


def check(what, ok, detail=""):
    global failed
    failed += not ok
    print("%s %s%s" % ("ok  " if ok else "FAIL", what, ("  (%s)" % detail) if detail else ""))


work = tempfile.mkdtemp(prefix="porthole-system-")
os.makedirs(os.path.join(work, "a"))
with open(os.path.join(work, "a", "make.conf"), "w") as f:
    f.write('''# made by catalyst
COMMON_FLAGS="-O2 -pipe"
CFLAGS="${COMMON_FLAGS}"

# Desktop
# (the main things)
USE="X gtk \\
     -qt5 dbus"

# not a heading: separated by a blank line

USE="${USE} alsa"   # trailing comment
export MAKEOPTS='-j3 -l4'
# Sound
USE="$USE -pulseaudio jack"
VIDEO_CARDS="intel"
VIDEO_CARDS="intel i965"
''')
conf = system.read_make_conf(os.path.join(work, "a"))
groups = [(g["title"], g["notes"], g["flags"]) for g in conf["use_groups"]]
check("groups with their comments", groups == [
    ("Desktop", ["(the main things)"], ["X", "gtk", "-qt5", "dbus"]),
    ("USE flags", [], ["alsa"]),
    ("Sound", [], ["-pulseaudio", "jack"])], groups)
check("references kept for other variables", conf["vars"]["CFLAGS"]["text"] == "${COMMON_FLAGS}")
check("export and single quotes", conf["vars"]["MAKEOPTS"]["text"] == "-j3 -l4",
      conf["vars"]["MAKEOPTS"]["text"])
check("last assignment wins", conf["vars"]["VIDEO_CARDS"]["text"] == "intel i965")

# make.conf as a directory: its files in order
os.makedirs(os.path.join(work, "b", "make.conf"))
for name, text in (("10-flags", '# Base\nUSE="a b"\n'), ("20-more", '# More\nUSE="${USE} c"\n'),
                   (".hidden", 'USE="nope"\n')):
    with open(os.path.join(work, "b", "make.conf", name), "w") as f:
        f.write(text)
conf = system.read_make_conf(os.path.join(work, "b"))
check("make.conf directory, hidden files skipped",
      [(g["title"], g["flags"]) for g in conf["use_groups"]] == [("Base", ["a", "b"]),
                                                                 ("More", ["c"])])

facts = {"threads": 4, "memory_gb": 7.6, "cpu_flags": ["sse", "avx", "f16c"]}
conf = {"use_groups": [{"flags": ["gtk", "gtk", "nothing-has-this", "-qt5"]}],
        "vars": {"MAKEOPTS": {"text": "-j6"}, "CPU_FLAGS_X86": {"text": "sse avx"}}}
found = dict(system.findings(conf, facts, {"gtk", "qt5"}))
check("flag no package has", "nothing-has-this" in found.get("Flags no package uses", ""))
check("flag set twice", "gtk" in found.get("Flags set twice", ""))
check("CPU flags missing", "f16c" in found.get(
    "CPU_FLAGS_X86 differs from what cpuid2cpuflags finds", ""))
check("too many jobs", "-j3" in found.get(
    "MAKEOPTS runs more jobs than this machine handles well", ""))
check("jobs advice", system.jobs_advice("-j 2", facts)[:2] == (2, 3))

# categories: the table, then words in the description, then specific/other
described = {"newcodec": "Enable the foo video decoder", "oddball": "Something unusual",
             "localthing": "Build the thing (app-misc/foo)"}
local = {"localthing": ["app-misc/foo"]}
check("table", system.categorise("pulseaudio", described, local) == "Audio")
check("description words", system.categorise("newcodec", described, local) == "Video and codecs")
check("package specific", system.categorise("localthing", described, local) ==
      system.SPECIFIC)
check("other", system.categorise("oddball", described, local) == system.OTHER)
check("every table category in the order", all(
    t in system.category_order() for t, _f in system.CATEGORIES))

# Porthole's global USE file: written, read back, refused, removed
import json  # noqa: E402
from porthole.updater import runner, targets  # noqa: E402
root = os.path.join(work, "c")
os.makedirs(os.path.join(root, "etc", "portage", "package.use"))
target = targets.Target("c", "c", root, root)
targets.get = lambda name=None: target
model = {"use": {"opus": False, "foo": True}, "expand": {"CPU_FLAGS_X86": ["aes", "avx"]}}
check("written", runner.set_global_use(json.dumps(model), "c") == "written")
check("reads back", system.read_global_use(target.portage_dir) == model,
      system.read_global_use(target.portage_dir))
for bad in ({"use": {"two words": True}}, {"use": {"x": "yes"}},
            {"expand": {"LD_PRELOAD": ["x"]}}, {"expand": {"VIDEO_CARDS": ["a;b"]}}, []):
    try:
        runner.set_global_use(json.dumps(bad), "c")
        check("refused %s" % json.dumps(bad), False)
    except runner.JobError:
        check("refused %s" % json.dumps(bad), True)
check("make.conf not touched", not os.path.exists(os.path.join(root, "etc", "portage", "make.conf")))
check("nothing left: file removed",
      runner.set_global_use(json.dumps({}), "c") == "removed"
      and not os.path.exists(system.global_use_path(target.portage_dir)))

# an overlay without use.local.desc: its flags from metadata.xml and IUSE
overlay = os.path.join(work, "overlay")
os.makedirs(os.path.join(overlay, "profiles"))
os.makedirs(os.path.join(overlay, "sys-apps", "board-meta"))
with open(os.path.join(overlay, "sys-apps", "board-meta", "board-meta-1.ebuild"), "w") as f:
    f.write('EAPI=8\nIUSE="+wifi kernel-bin ${EXTRA}"\nIUSE+=" fbcondecor"\n')
with open(os.path.join(overlay, "sys-apps", "board-meta", "metadata.xml"), "w") as f:
    f.write('<pkgmetadata><use><flag name="fbcondecor">Console backgrounds</flag></use>'
            '</pkgmetadata>\n')
said, local = system.flag_descriptions([overlay])
check("overlay flags found without use.local.desc",
      sorted(local) == ["fbcondecor", "kernel-bin", "wifi"], local)
check("overlay flag text from metadata.xml",
      said.get("fbcondecor") == "Console backgrounds (sys-apps/board-meta)", said)
sys.exit(1 if failed else 0)
