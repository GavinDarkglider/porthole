"""
    Porthole: colours that fit the GTK theme

    Built-in colours are worked out from the theme in use (its accent,
    error and background colours) and made readable on it: a colour is
    made lighter or darker until its contrast with the background is at
    least WCAG's 4.5:1. Colours the user picked are used as they are.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import colorsys

MINIMUM = 4.5
_cache = {}


def _gtk():
    import gi
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk
    return Gdk, Gtk


def parse(spec):
    """(r, g, b) 0..1 of a colour name or #hex, or None"""
    if not spec:
        return None
    Gdk, _Gtk = _gtk()
    rgba = Gdk.RGBA()
    if not rgba.parse(spec):
        return None
    return (rgba.red, rgba.green, rgba.blue)


def hex_of(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in rgb)


def luminance(rgb):
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable(colour, background, minimum=MINIMUM):
    """colour, made lighter (on a dark background) or darker (on a light
    one) until it reads well against background; both (r, g, b)"""
    if contrast(colour, background) >= minimum:
        return colour
    h, lightness, s = colorsys.rgb_to_hls(*colour)
    lighter = luminance(background) < 0.18
    for _ in range(40):
        lightness = min(1.0, lightness + 0.025) if lighter else max(0.0, lightness - 0.025)
        candidate = colorsys.hls_to_rgb(h, lightness, s)
        if contrast(candidate, background) >= minimum or lightness in (0.0, 1.0):
            return candidate
    return candidate


def _theme_key():
    _Gdk, Gtk = _gtk()
    settings = Gtk.Settings.get_default()
    if settings is None:
        return None
    return (settings.props.gtk_theme_name,
            settings.props.gtk_application_prefer_dark_theme)


def palette():
    """The theme's colours: text, base (list background), accent, error"""
    key = _theme_key()
    if key in _cache:
        return _cache[key]
    _Gdk, Gtk = _gtk()
    context = Gtk.TreeView().get_style_context()
    text = context.get_color(Gtk.StateFlags.NORMAL)
    text = (text.red, text.green, text.blue)
    dark = luminance(text) > 0.4

    def named(*names, default):
        for name in names:
            found, rgba = context.lookup_color(name)
            if found:
                return (rgba.red, rgba.green, rgba.blue)
        return parse(default)
    colours = {
        "text": text,
        "base": named("theme_base_color", "base_color",
                      default="#2d2d2d" if dark else "#ffffff"),
        "accent": named("theme_selected_bg_color", "selected_bg_color", default="#3584e4"),
        "error": named("error_color", default="#e01b24"),
    }
    _cache[key] = colours
    return colours


def auto(kind):
    """A built-in colour for kind ("upgrade", "downgrade"), as #hex"""
    colours = palette()
    source = colours["accent"] if kind == "upgrade" else colours["error"]
    return hex_of(readable(source, colours["base"]))


def fit_tags(tags):
    """Terminal tag defaults {name: [fg, bg, weight]} made readable on
    this theme: a text colour against its own background (or the
    terminal's), a background against the text drawn on it"""
    colours = palette()
    fitted = {}
    for name, (fg, bg, weight) in tags.items():
        fg_rgb, bg_rgb = parse(fg), parse(bg)
        if fg_rgb:
            fg = hex_of(readable(fg_rgb, bg_rgb or colours["base"]))
        elif bg_rgb:
            # the theme's text goes on it: keep the background clear of it
            bg = hex_of(readable(bg_rgb, colours["text"]))
        fitted[name] = [fg, bg, weight]
    return fitted
