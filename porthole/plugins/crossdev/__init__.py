"""
    Porthole crossdev plugin (GUI half)

    Builds whole systems for other computers (another architecture, like a
    Nintendo Switch or a Raspberry Pi) from this one: crossdev toolchains
    worked out from the target's own profile, cross-compiling first and
    emulation only for what won't cross-compile, with distcc so emulated
    builds still compile natively. This is the start: checking that this
    computer is ready.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

TITLE = "Build systems for other computers (crossdev)"
DESCRIPTION = ("Cross-compile a whole Gentoo system for another architecture with "
               "crossdev, falling back to emulation (qemu) for what won't "
               "cross-compile, and manage it with Porthole afterwards.")

# what the old plugin loader reads
desc = DESCRIPTION
is_installed = True

_state = {"manager": None, "menuitems": []}


def _load(manager):
    _state["manager"] = manager
    return True


def _enable():
    manager = _state["manager"]
    if manager is None or _state["menuitems"]:
        return True
    for label, action in (("Systems for other computers…", _systems),
                          ("Ready to build for another computer?", _readiness)):
        item = manager.new_menuitem(label)
        item.connect("activate", lambda w, a=action: a())
        item.show()
        _state["menuitems"].append(item)
    return True


def _disable():
    if _state["manager"] is not None:
        for item in _state["menuitems"]:
            _state["manager"].del_menuitem(item)
    _state["menuitems"] = []
    return True


def _systems():
    from porthole.plugins.crossdev.window import SystemsWindow
    SystemsWindow(getattr(_state["manager"], "porthole_instance", None))


def _readiness():
    from porthole.plugins.crossdev.window import ReadinessWindow
    manager = _state["manager"]
    ReadinessWindow(getattr(manager, "porthole_instance", None))


event_table = {"load": _load, "enable": _enable, "disable": _disable, "unload": _disable}
