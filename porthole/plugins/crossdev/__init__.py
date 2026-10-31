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

_state = {"manager": None, "menuitem": None}


def _load(manager):
    _state["manager"] = manager
    return True


def _enable():
    manager = _state["manager"]
    if manager is None or _state["menuitem"] is not None:
        return True
    item = manager.new_menuitem("Ready to build for another computer?")
    item.connect("activate", lambda w: _readiness())
    item.show()
    _state["menuitem"] = item
    return True


def _disable():
    if _state["menuitem"] is not None and _state["manager"] is not None:
        _state["manager"].del_menuitem(_state["menuitem"])
    _state["menuitem"] = None
    return True


def _readiness():
    from porthole.plugins.crossdev.window import ReadinessWindow
    ReadinessWindow()


event_table = {"load": _load, "enable": _enable, "disable": _disable, "unload": _disable}
