#!/usr/bin/env python

'''
    Porthole command line entry Dialog
    Shows information about Porthole

    Copyright (C) 2003 - 2026 Fredrik Arnerup, Daniel G. Taylor,
    Brian Dolbec and William F. Wheeler

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program; if not, write to the Free Software
    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
'''

from gi.repository import Gtk
from gettext import gettext as _

from porthole.utils import debug
from porthole.loaders.loaders import load_web_page
from porthole import config

class RunDialog:
    """Class to hold run dialog and functionality."""

    # Define our response IDs (positive ints → GTK4-friendly)
    # keeping gtk3 values for now
    RESPONSE_HELP  = -11
    RESPONSE_APPLY = -10
    RESPONSE_ACCEPT = -3
    RESPONSE_CANCEL = -6 # Gtk.ResponseType.CANCEL still -6, ok for now
    RESPONSE_OK = -5     # Gtk.ResponseType.OK     still -5
    RESPONSE_CLOSE = -4

    def __init__(self, call_back, run_anyway=False):
        # setup glade
        self.gladefile = config.Prefs.DATA_PATH + "/glade/run_dialog.ui"
        self.builder = Gtk.Builder()
        self.builder.add_from_file(self.gladefile)
        self.builder.set_translation_domain(config.Prefs.APP)
        self.window = self.builder.get_object("run_dialog")
        # register callbacks
        callbacks = {"on_help" : self.help,
                     "on_execute" : self.execute,
                     "on_cancel" : self.cancel,
                     "on_comboboxentry1_changed" : self.command_changed
                    }
        self.builder.connect_signals(callbacks)
        self.command = None
        self.call_back = call_back
        self.run_anyway = run_anyway
        if config.Prefs:
            #debug.dprint("COMMAND: config.Prefs == True")
            self.history = config.Prefs.run_dialog.history
        else:
            debug.dprint("COMMAND: config.Prefs == False")
            self.history = ["", "emerge ",
                            "ACCEPT_KEYWORDS='~x86' emerge ",
                            "USE=' ' emerge ",
                            "ACCEPT_KEYWORDS='~x86' USE=' ' emerge"]
        debug.dprint("COMMAND: self.history:"+ str(self.history))
        #self.window = self.builder.get_object("run_dialog")
        self.combo = self.builder.get_object("comboboxentry1")
        debug.dprint("COMMAND: combo has_entry: " + str(self.combo.props.has_entry))
        for x in self.history:
            debug.dprint("COMMAND: x = " + x)
            # Add the entry
            self.combo.append_text(x)
        self.combo.set_active(0) # by index
        self.combo.set_size_request(800, -1)  # width 800px, height natural
        self.window.connect("response", self.on_response)
        if config.Prefs:
            self.window.set_default_size(config.Prefs.run_dialog.width,
                config.Prefs.run_dialog.height)

            self.window.connect("configure-event", self.on_configure)
        self.window.set_default_response(self.RESPONSE_CANCEL)

    def on_response(self, dialog_widget, response_id):
        """ Parse dialog response (ok, cancel, apply or help clicked) """
        debug.dprint("COMMAND: on_config_response(): response_id '%s'" % response_id)
        if response_id in (self.RESPONSE_CANCEL, self.RESPONSE_CLOSE):
            debug.dprint("COMMAND: on_response(): Cancel response_id '%s'" % response_id)
            self.window.destroy()
        elif response_id == self.RESPONSE_ACCEPT:
            debug.dprint("COMMAND: on_response(): Execute response_id '%s'" % response_id)
            self.execute()
            self.window.destroy()
        elif response_id == self.RESPONSE_HELP:
            debug.dprint("COMMAND: on_response(): Help response_id '%s'" % response_id)
            #Display help file with web browser
            load_web_page('file://' + config.Prefs.DATA_PATH + 'help/customize.html')
        else:
            debug.dprint(f"COMMAND: Unhandled response: {response_id}")

    def execute(self):
        """Adds the command line entry to the queue"""
        # Get command from combo
        command = self.combo.get_active_text().strip() or (self.combo.get_child().get_text() if self.combo.props.has_entry else "")
        if command and command not in self.history:
            self.history.insert(0, command)  # Add to top
            self.combo.prepend_text(command)  # Add to combo visually
            # Optional: limit history length
            if len(self.history) > config.Prefs.run_dialog.history_length:
                self.history.pop()
                # Remove oldest from combo if needed
        if command:
            self.command = command
            #debug.dprint("COMMAND: execute: %s" %self.command)
            self.call_back(_("command line entry"), self.command, self.run_anyway)
            self.history_add()
        else:
            debug.dprint("No command entered – ignoring")
        self.cancel(None)

    def cancel(self, widget):
        """cancels run dialog"""
        self.window.destroy()

    def help(self, widget):
        """ Display help file with web browser """
        load_web_page('file://' + config.Prefs.DATA_PATH + 'help/custcmd.html', config.Prefs)

    # def on_size_request(self, window, gbox):
        # """ Store new size in prefs """
        # # get the width and height of the window
        # width, height = window.get_size()
        # # set the preferences
        # config.Prefs.run_dialog.width = width
        # config.Prefs.run_dialog.height = height

    def on_configure(self, widget, event):
        # Save size when resized
        if config.Prefs:
            config.Prefs.run_dialog.width = event.width
            config.Prefs.run_dialog.height = event.height
        return False  # propagate

    def history_add(self):
        """adds the command to the history if not already in"""
        # if self.command not in self.history:
            # length = len(self.history)
            # if length > config.Prefs.run_dialog.default_history:
                # length = min(length, config.Prefs.run_dialog.history_length)
                # old_history = self.history[config.Prefs.run_dialog.default_history:length]
                # self.history = self.history[:config.Prefs.run_dialog.default_history]
                # self.history.append(self.command)
                # self.history += old_history
            # else:
                # self.history.append(self.command)
        debug.dprint("COMMAND.history_add(): new self.history:")
        debug.dprint("COMMAND:" + str(self.history))
        config.Prefs.run_dialog.history = self.history
        return

    def command_changed(self,widget):
        """Updates the Gtk.Entry with the history item selected"""
        debug.dprint("COMMAND: changing entry item")
        return # not needed at this time
        model = widget.get_model()
        iter = widget.get_active_iter()
        # fixme unused selection
        selection = model.get_value(iter, 0)

