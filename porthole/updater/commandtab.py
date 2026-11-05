#!/usr/bin/env python3

'''
    Porthole: the job window's Commands tab

    Shows a job's command record (recipe.py: <job>.sh next to its log),
    growing as the job runs: the job selected in the queue, else the one
    the window followed last.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
'''

import os
import shutil
from gettext import gettext as _

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from porthole.updater import recipe

try:
    gi.require_version("GtkSource", "4")
    from gi.repository import GtkSource
except (ValueError, ImportError):
    GtkSource = None

TITLE = _("Commands")
EXPLAIN = _("Every command this job ran, with its settings, as a bash script with notes "
            "on what each step does: a way to see how Gentoo builds, step by step.")
REFRESH = 2  # seconds


class CommandsTab:
    def __init__(self, notebook, job_for):
        """notebook: the job window's; job_for(): the job to show (or None)"""
        self.notebook, self.job_for = notebook, job_for
        self.path, self.offset = None, 0
        self.page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.page.set_border_width(4)

        top = Gtk.Box(spacing=6)
        self.label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.label.set_tooltip_text(EXPLAIN)
        top.pack_start(self.label, True, True, 0)
        self.open_button = Gtk.Button(label=_("Open in editor"))
        self.open_button.set_tooltip_text(_("Open the script in your text editor"))
        self.open_button.connect("clicked", self.open_editor)
        top.pack_end(self.open_button, False, False, 0)
        self.save_button = Gtk.Button(label=_("Save a copy..."))
        self.save_button.connect("clicked", self.save_copy)
        top.pack_end(self.save_button, False, False, 0)
        self.page.pack_start(top, False, False, 0)

        if GtkSource is not None:
            self.buffer = GtkSource.Buffer()
            language = GtkSource.LanguageManager.get_default().get_language("sh")
            if language is not None:
                self.buffer.set_language(language)
            self.view = GtkSource.View(buffer=self.buffer, show_line_numbers=True)
        else:
            self.buffer = Gtk.TextBuffer()
            self.view = Gtk.TextView(buffer=self.buffer)
        self.tags = {}
        if GtkSource is None:
            # no syntax colouring: notes, headings and failures told apart by
            # style, in the theme's own colours
            from gi.repository import Pango
            from porthole.utils import themecolours
            colours = themecolours.palette()
            text = themecolours.hex_of(tuple(
                t * 0.6 + b * 0.4 for t, b in zip(colours["text"], colours["base"])))
            error = themecolours.hex_of(themecolours.readable(colours["error"], colours["base"]))
            self.tags = {
                "note": self.buffer.create_tag("note", style=Pango.Style.ITALIC,
                                               foreground=text),
                "heading": self.buffer.create_tag("heading", weight=Pango.Weight.BOLD),
                "failed": self.buffer.create_tag("failed", weight=Pango.Weight.BOLD,
                                                 foreground=error)}
        self.view.set_editable(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.add(self.view)
        self.page.pack_start(self.scroller, True, True, 0)

        tab = Gtk.Box(spacing=4)
        tab.pack_start(Gtk.Image.new_from_icon_name("utilities-terminal", Gtk.IconSize.MENU),
                       False, False, 0)
        tab.pack_start(Gtk.Label(label=TITLE), False, False, 0)
        tab.set_tooltip_text(EXPLAIN)
        tab.show_all()
        self.page.show_all()
        self.notebook.append_page(self.page, tab)
        # stays last, after the queue tab (which comes and goes)
        self.notebook.connect("page-added", self.keep_last)
        self.refresh()
        self.timer = GLib.timeout_add_seconds(REFRESH, self.refresh)
        self.page.connect("destroy", self.stop)

    def keep_last(self, notebook, child, number):
        if child is not self.page:
            notebook.reorder_child(self.page, -1)

    def stop(self, *args):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def _at_end(self):
        adjustment = self.scroller.get_vadjustment()
        return adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 20

    def refresh(self):
        try:
            job = self.job_for()
        except Exception:
            job = None
        path = recipe.record_path(job["log"]) if job and job.get("log") else None
        if path != self.path:
            self.path, self.offset = path, 0
            self.buffer.set_text("")
        if path is None:
            self.label.set_text(_("No job to show."))
            self._buttons(False)
            return True
        if not os.path.exists(path):
            self.label.set_text(_("%s: no command record (jobs from before this "
                                  "Porthole don't have one).") % job.get("title", ""))
            self._buttons(False)
            return True
        self.label.set_markup("<b>%s</b>  %s" % (
            GLib.markup_escape_text(job.get("title", "")), GLib.markup_escape_text(path)))
        self._buttons(True)
        try:
            size = os.path.getsize(path)
            if size < self.offset:  # written again from the start
                self.offset = 0
                self.buffer.set_text("")
            if size == self.offset:
                return True
            with open(path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return True
        # whole lines only; the rest comes next time
        cut = data.rfind(b"\n") + 1
        if not cut:
            return True
        self.offset += cut
        follow = self._at_end()
        self._insert(data[:cut].decode(errors="replace"))
        if follow:
            GLib.idle_add(lambda: self.view.scroll_to_iter(self.buffer.get_end_iter(),
                                                           0, False, 0, 1) and False)
        return True

    def _insert(self, text):
        if not self.tags:
            self.buffer.insert(self.buffer.get_end_iter(), text)
            return
        for line in text.splitlines(True):
            if "   # failed (exit status" in line:
                tag = "failed"
            elif line.startswith("# Step ") or line.startswith("# --- ") or \
                    line.startswith("# How it went") or line.startswith("# ====="):
                tag = "heading"
            elif line.startswith("#"):
                tag = "note"
            else:
                tag = None
            if tag:
                self.buffer.insert_with_tags(self.buffer.get_end_iter(), line, self.tags[tag])
            else:
                self.buffer.insert(self.buffer.get_end_iter(), line)

    def _buttons(self, sensitive):
        self.save_button.set_sensitive(sensitive)
        self.open_button.set_sensitive(sensitive)

    def save_copy(self, *args):
        if not self.path:
            return
        dialog = Gtk.FileChooserDialog(title=_("Save the command record"),
                                       transient_for=self.page.get_toplevel(),
                                       action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(os.path.basename(self.path))
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                shutil.copyfile(self.path, dialog.get_filename())
            except OSError as e:
                message = Gtk.MessageDialog(transient_for=dialog, modal=True,
                                            message_type=Gtk.MessageType.ERROR,
                                            buttons=Gtk.ButtonsType.CLOSE,
                                            text=_("Couldn't save it: %s") % e)
                message.run()
                message.destroy()
        dialog.destroy()

    def open_editor(self, *args):
        if not self.path:
            return
        try:
            # text/plain, not the script's own type: opened to read, never run
            from gi.repository import Gio
            app = Gio.AppInfo.get_default_for_type("text/plain", False)
            if app is not None:
                app.launch([Gio.File.new_for_path(self.path)], None)
            else:
                Gtk.show_uri_on_window(self.page.get_toplevel(),
                                       "file://" + self.path, Gtk.get_current_event_time())
        except GLib.Error:
            pass
