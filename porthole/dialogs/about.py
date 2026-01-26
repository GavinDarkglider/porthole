#!/usr/bin/env python

'''
    Porthole About Dialog
    Shows information about Porthole

    Copyright (C) 2003 - 2026 Fredrik Arnerup, Daniel G. Taylor, Brian Dolbec

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

from porthole.loaders.loaders import get_textfile

from porthole.version import (
    copyright,
    version,
)
from porthole import config
from porthole import backends
from porthole.utils import debug

class AboutDialog:
    """Class to hold about dialog and functionality."""

    def __init__(self, parent=None):
        self.gladefile = config.Prefs.DATA_PATH + 'glade/about.ui' #config.Prefs.use_gladefile
        self.builder = Gtk.Builder()
        try:
            self.builder.add_from_file(self.gladefile)
        except Exception as e:
            debug.dprint(f"Failed to load about.ui: {e}")

        self.builder.set_translation_domain(config.Prefs.APP)
        self.window = self.builder.get_object("about")
        self.window.set_resizable(True)

        if parent:
            self.window.set_transient_for(parent)
            self.window.set_modal(True)

        try:
            license_file = backends.portage_lib.settings.portdir + "/licenses/GPL-2"
            license_txt = get_textfile(license_file)
            if license_txt:
                self.window.set_license(license_txt)
                self.window.set_license_type(Gtk.License.CUSTOM)
        except Exception as e:
            debug.dprint(f"Failed to load/set license: {e}")

        try:
            author_file = config.Prefs.AUTHORS
            authors = get_textfile(author_file).split('\n')
            self.window.set_property("authors", authors)
        except Exception as e:
            debug.dprint(f"Failed to load/set authors: {e}")

        try:
            translator_file = config.Prefs.TRANSLATORS
            translators = get_textfile(translator_file)
            self.window.set_property("translator_credits", translators)
        except Exception as e:
            debug.dprint(f"Failed to load/set translators: {e}")


        self.window.set_property("version", version)
        self.window.set_copyright(copyright)
        self.window.set_license(get_textfile(license_file))
        self.window.connect("response", self.on_response)

    def run(self):
        debug.dprint(f"ABOUT: showing window...")
        self.window.show_all()

    def on_response(self, dialog, response_id):
        dialog.destroy()
