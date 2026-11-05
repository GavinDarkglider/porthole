"""
    Porthole update assistant: fully automatic updates (automation = auto)

    The planner's safe decisions, so an update can go ahead with nobody
    there, and one news item per update run saying what was decided, why,
    and how to change it.

    What it accepts by itself (the planner verified each change works):
      - USE changes the update needs
      - keyword changes (testing versions) the update needs, if the user
        allowed them (auto_testing in updater.conf)
      - temporary USE changes that break a circular dependency (made for
        one stage, then taken out again)

    What it never does by itself, holding back what needed it instead:
      - accepting a licence (it can be a legal agreement)
      - unmasking a version (masks usually mean known broken or unsafe)
      - "unlocks": testing versions that would free held-back updates
      - switching to a new GCC, removing or replacing installed software
      - removing packages nothing needs (unless the user ticked them)

    Those refusals are one-off choices for this run: they never go into
    the user's standing choices, so the next check asks again (and a
    person opening Updates sees them offered as usual).

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import textwrap
import time

from porthole.updater.analyze import CONFIG_DIRS, OWNED_NAME, normalize_choices

# changes the planner may make by itself, once verified
ACCEPTED_KINDS = ("use", "keywords")
# planning again with its own refusals settles in a round or two
MAX_ROUNDS = 4


def refusals(plan, allow_testing=False):
    """What in a plan the autopilot won't accept: ({choice list: [values]},
    {value: what it was, for the news})"""
    accepted = ACCEPTED_KINDS if allow_testing else ("use",)
    found, seen = {"reject_fixes": [], "decline": []}, {}
    for fix in plan.get("fixes") or []:
        if fix.get("requested"):
            continue  # the user's own
        if fix.get("unlocks") or fix["kind"] not in accepted:
            found["reject_fixes"].append(fix["line"])
            seen[fix["line"]] = {"type": "fix", **fix}
    for stage in plan.get("stages") or []:
        if stage.get("approval") and stage.get("key") and stage["key"] not in seen:
            # a switch has several stages under one key: the first names it
            found["decline"].append(stage["key"])
            seen[stage["key"]] = {"type": "stage", **stage}
    return found, seen


def plan_by_itself(analyze, standing, log=lambda m: None, allow_testing=False):
    """Plan with the autopilot's decisions: analyze(choices) -> plan.
    Returns (plan, refused) where refused maps each choice value it added
    to what it was."""
    extra = {"reject_fixes": [], "decline": []}
    refused = {}
    plan = None
    for _ in range(MAX_ROUNDS):
        choices = normalize_choices(standing)
        for key, values in extra.items():
            choices[key] = sorted(set(choices[key]) | set(values))
        plan = analyze(choices)
        found, seen = refusals(plan, allow_testing)
        new = {k: [v for v in vs if v not in extra[k]] for k, vs in found.items()}
        if not any(new.values()):
            break
        for key, values in new.items():
            extra[key] += values
        refused.update(seen)
        log("Deciding by itself: holding back what needs "
            + "; ".join(v for vs in new.values() for v in vs))
    # its refusals are not the user's choices: kept apart, so the review
    # window doesn't offer them as such (or save them as standing choices)
    held = [h for h in plan.get("held_by_you") or []
            if (h.get("undo") or {}).get("value") in refused]
    plan["held_by_you"] = [h for h in plan.get("held_by_you") or [] if h not in held]
    plan["choices"] = normalize_choices(standing)
    plan["auto"] = {"refused": sorted(refused), "held": held}
    return plan, refused


# -- the news item ---------------------------------------------------------------

NEWS_WIDTH = 72


def _para(text, indent=""):
    return textwrap.fill(" ".join(text.split()), NEWS_WIDTH, initial_indent=indent,
                         subsequent_indent=" " * len(indent), break_on_hyphens=False,
                         break_long_words=False)


def _sentences(lines):
    """The planner's explanation lines as sentences"""
    text = " ".join(" ".join(lines or []).split())
    return text + "." if text and text[-1] not in ".!?" else text


def _file_for(kind, portage_dir="/etc/portage"):
    return "%s/%s/%s" % (portage_dir, CONFIG_DIRS.get(kind, "package." + kind), OWNED_NAME)


def _refused_text(item, portage_dir="/etc/portage"):
    """(what was done, why, how to change it) for a refusal"""
    if item["type"] == "stage":
        key = item.get("key", "")
        if key == "gcc-switch":
            return ("Left the compiler as it is: %s" % item["title"],
                    "Switching to a new GCC changes how everything after it is built, "
                    "so it is always your decision.",
                    "Open Updates and click Check now: the switch is offered there "
                    "for you to approve.")
        if key.startswith(("switch-gcc:", "switch-profile:")):
            return ("Left it as it is: %s" % item["title"],
                    "Switching the profile or compiler rebuilds everything, so it is "
                    "always your decision.",
                    "Open Updates: the switch is offered there for you to choose.")
        if key.startswith("remove:"):
            return ("Did not remove %s; what it holds back stays at its installed "
                    "version." % key.split(":", 1)[1],
                    item.get("why") or "",
                    "Porthole never removes software by itself. To remove it, open "
                    "Updates, click Check now and approve the removal there.")
        return ("Did not let the update replace %s; the update that needs that is "
                "held back." % key.split(":", 1)[1],
                item.get("why") or "",
                "Replacing installed software is your decision. Open Updates, click "
                "Check now and approve it there if you want it.")
    why = _sentences(item.get("explanation"))
    if item.get("unlocks"):
        return ("Did not add '%s' to %s, so %s stays held back."
                % (item["line"], _file_for(item["kind"], portage_dir), item["unlocks"]),
                why + " It would mean using a testing version, which is your call.",
                "Open Updates and click Check now: it is offered there for held-back "
                "updates, for you to tick.")
    reason = {
        "keywords": "This is a testing version, and automatic updates are set not to "
                    "use testing versions (Settings, Updates).",
        "licenses": "Accepting a licence can be a legal agreement, so Porthole never "
                    "does it for you.",
        "unmask": "Masked versions are usually known to be broken or unsafe, so "
                  "Porthole never unmasks one by itself.",
    }.get(item["kind"], "Porthole doesn't make this kind of change by itself.")
    return ("Did not add '%s' to %s; what needed it is held back."
            % (item["line"], _file_for(item["kind"], portage_dir)),
            (why + " " if why else "") + reason,
            "Open Updates and click Check now: the change is offered there for you "
            "to approve.")


def news_text(plan, refused, window_text, job_id=None):
    """(title, body) of the news item for an update the autopilot queued
    (or couldn't)"""
    from porthole.updater.targets import of_plan
    portage_dir = of_plan(plan).portage_dir
    merges = plan.get("merge_list") or []
    rebuilds = (plan.get("preserved") or {}).get("merge_list") or []
    lives = (plan.get("live") or {}).get("changed") or []
    ready = plan.get("status") in ("ready", "needs-approval")
    nothing = plan.get("status") == "up-to-date"
    count = len(merges) + len(rebuilds) + len(lives)
    if ready:
        title = "Automatic update: %d package%s, built %s" % (
            count, "" if count == 1 else "s", window_text)
    elif nothing:
        title = "Automatic update: nothing to build, %d thing%s left for you" % (
            len(refused), "" if len(refused) == 1 else "s")
    else:
        title = "Automatic update: it needs you"
    out = []

    if nothing:
        out.append(_para(
            "Everything Porthole could update by itself is up to date. What is "
            "left needs a decision only you can make, so nothing was built."))
    elif ready:
        out.append(_para(
            "Porthole worked out this update by itself and queued it for the build "
            "window (%s). It starts once nobody is using the computer, and pauses if "
            "someone comes back. To stop it, open Updates and click Cancel update."
            % window_text))
    else:
        from porthole.updater.analyze import summarize
        heading, body = summarize(plan)
        out.append(_para(
            "Porthole couldn't work out an update it can run by itself, so nothing "
            "will be built until you look at it: open Updates."))
        out.append("")
        out.append(_para(heading + (" " + body if body else "")))

    if merges or rebuilds or lives:
        out += ["", "What it updates", "---------------"]
        for m in merges:
            out.append("  %s" % m["cpv"])
        for m in rebuilds:
            out.append("  %s (rebuilt: it uses an old library)" % m["cpv"])
        for c in lives:
            out.append("  %s (live package: new upstream commits)" % c["cpv"])

    accepted = [f for f in plan.get("fixes") or [] if not f.get("requested")]
    temp = next((s["config"]["use"] for s in plan.get("stages") or []
                 if s.get("config")), None)
    if accepted or temp:
        out += ["", "Changes it made to get the update through",
                "-----------------------------------------"]
        for fix in accepted:
            out.append("")
            out.append(_para("Added '%s' to %s." % (fix["line"],
                                                    _file_for(fix["kind"], portage_dir)),
                             "* "))
            why = _sentences(fix.get("explanation"))
            if why:
                out.append(_para("Why: " + why, "  "))
            out.append(_para(
                "Checked: Porthole asked portage to work out the whole update with "
                "this change, and it did.", "  "))
            out.append(_para(
                "To change it: the line is marked with a comment in that file. Delete "
                "or edit it after the update; the next check then works out what "
                "that means and tells you.", "  "))
        if temp:
            out.append("")
            out.append(_para(
                "Used temporary USE changes to get round a circular dependency: %s. "
                "They are set for the first build only and taken out again, then the "
                "packages are rebuilt with your own settings." % "; ".join(temp), "* "))
            out.append(_para(
                "Why: some packages need each other to build; turning a feature off "
                "for one build is the usual way out. Nothing for you to do.", "  "))

    if refused:
        out += ["", "What it left for you", "--------------------"]
        for value in sorted(refused):
            did, why, change = _refused_text(refused[value], portage_dir)
            out.append("")
            out.append(_para(did, "* "))
            if why:
                out.append(_para("Why: " + why, "  "))
            out.append(_para("To change it: " + change, "  "))

    held = plan.get("held_back") or []
    if held:
        out += ["", "Held back (can't be updated yet)", "--------------------------------"]
        for h in held:
            out.append("")
            out.append(_para("%s: %s" % (h["cpv"], " ".join(h.get("why") or [])
                                         or h.get("reason", "")), "* "))
            labels = [o["label"] for o in h.get("options") or []]
            if labels:
                out.append(_para("Your options, in Updates: " + "; ".join(labels) + ".",
                                 "  "))

    unneeded = [p for p in (plan.get("unneeded") or {}).get("packages") or []
                if not p["remove"]]
    if unneeded:
        out += ["", "No longer needed", "----------------"]
        out.append(_para(
            "Nothing needs these any more: %s. Porthole never removes software by "
            "itself; tick them in Updates to remove them."
            % ", ".join(p["cpv"] for p in unneeded)))

    out += ["", _para(
        "Porthole decides like this because Settings, Updates is set to fully "
        "automatic. It accepts only changes it has checked, never accepts licences, "
        "unmasks, compiler switches or removals by itself, and writes one of these "
        "notes for each update." + (" (Job %s.)" % job_id if job_id else ""))]
    return title, "\n".join(out) + "\n"


def stamp():
    return time.strftime("%Y-%m-%d %H:%M")


def result_text(job):
    """The paragraph added to the news item when the build ends"""
    words = {"done": "Finished: everything in the update was built and installed.",
             "failed": "Failed: a step went wrong and the rest was skipped. The tray's "
                       "Show what failed (or Porthole, Job logs) shows why. Porthole "
                       "won't queue this same update by itself again; it waits for you.",
             "cancelled": "Cancelled: packages already built stay installed; the next "
                          "check works out what is left."}
    return "\n" + "Result (%s)\n%s\n%s\n" % (
        stamp(), "-" * (len(stamp()) + 9),
        _para(words.get(job["status"], job["status"])))
