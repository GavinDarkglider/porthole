"""
    Click through the update assistant on a fake system from scenarios.py:
    toggle check boxes by label, re-plan with the resulting choices, and
    screenshot each step (needs a display, xwininfo and ImageMagick).
    Re-planning runs the analyzer directly instead of through sudo and
    the job runner.

    Usage: review_ui.py PORTHOLE_SRC OUT_DIR SCENARIO [LABEL_PREFIX...]
      e.g. review_ui.py ~/porthole /tmp/shots held-stuck "Keep lib/v-1"
    Uses whichever porthole is importable (set PYTHONPATH for a checkout).

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""
import json, os, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1] + "/tests/updater")   # scenarios only
OUT, NAME = sys.argv[2], sys.argv[3]
import scenarios
import gi; gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
import porthole.updater.review as review
from porthole.updater.analyze import Analyzer
print("assistant from:", review.__file__)

work = tempfile.mkdtemp(prefix="held-ui-")
config, root = scenarios.build(work, scenarios.SCENARIOS[NAME])
plan_path = os.path.join(work, "plan.json")
targets = ["--update", "--deep", "--newuse", "@world", "--root=" + root, "--sysroot=" + root]

def analyze(choices):
    plan = Analyzer(list(targets), config, log=lambda m: None, choices=choices).analyze()
    json.dump(plan, open(plan_path, "w"))
    return plan

def describe(plan):
    return "%s | updates: %s | held back: %s | kept by you: %s | approvals: %s" % (
        plan["status"], [m["cpv"] for m in plan["merge_list"]],
        [h["cpv"] for h in plan.get("held_back") or []],
        [h["cp"] for h in plan.get("held_by_you") or []],
        [s["title"] for s in plan["stages"] if s.get("approval")])

# the assistant reads our plan; re-planning runs the analyzer with its choices
review.read_json = lambda path, default=None: json.load(open(plan_path)) if path == review.PLAN_FILE else default
review.stale_reason = lambda plan: None
review.read_jobs = lambda: []
def fake_root(argv, callback):
    choices = json.loads(argv[argv.index("--choices") + 1])
    print("  re-plan with", {k: v for k, v in choices.items() if v})
    print("  ->", describe(analyze(choices)))
    callback(True, "job")
review.run_as_root = fake_root
review.UpdateAssistant.show_progress = lambda self: None

def checks(w):
    if isinstance(w, Gtk.CheckButton): yield w
    if isinstance(w, Gtk.Container):
        for c in w.get_children(): yield from checks(c)
def text(b):
    c = b.get_child()
    return c.get_text() if isinstance(c, Gtk.Label) else "?"
def shot(name):
    while Gtk.events_pending(): Gtk.main_iteration()
    # this process's window: an installed Porthole may have one with the same title open
    out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True).stdout
    wid = next(l.split()[0] for l in out.splitlines()
               if '"Porthole: update assistant"' in l
               and subprocess.run(["xprop", "-id", l.split()[0], "_NET_WM_PID"], capture_output=True,
                                  text=True).stdout.split()[-1] == str(os.getpid()))
    subprocess.run(["import", "-window", wid, os.path.join(OUT, name)])
def boxes(a):
    return {text(b): b for b in checks(a.content)}

print("first plan ->", describe(analyze({})))
class MW:
    process_manager = None
    def configure_porthole(self, page=None): pass
a = review.UpdateAssistant(None, MW())
a.set_default_size(860, 760)
a.present_plan()
steps = [s for s in sys.argv[4:]]

def run_steps():
    shot(NAME + "-0.png")
    for i, step in enumerate(steps, 1):
        label = next((t for t in boxes(a) if t.startswith(step)), None)
        if label is None:
            print("STEP %d: no check box starting %r; have %s" % (i, step, list(boxes(a))))
            break
        box = boxes(a)[label]
        box.set_active(not box.get_active())
        print("STEP %d: toggled %r -> %s; re-plan bar shown: %s, Start enabled: %s"
              % (i, label, box.get_active(), a.choices_bar.get_visible(),
                 a.start_button.get_sensitive()))
        a.on_choices_response(a.choices_bar, 2)       # Re-plan with my choices
        a.load()
        print("  after re-plan: bar shown: %s, Start enabled: %s"
              % (a.choices_bar.get_visible(), a.start_button.get_sensitive()))
        shot("%s-%d.png" % (NAME, i))
    Gtk.main_quit()
    return False
GLib.timeout_add(1500, run_steps)
Gtk.main()
subprocess.run(["rm", "-rf", work])
