#!/usr/bin/env python

'''
    Porthole Configuration Dialog
    Allows the user to Configure Porthole

    Copyright (C) 2005 - 2026 Brian Dolbec, Thomas Iorns

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

import gi; gi.require_version("Gtk", "3.0") # make sure we have the right version
from gi.repository import Gtk
from gi.repository import Gdk

from porthole.utils import debug
from porthole.loaders.loaders import load_web_page
from porthole import config
from porthole import backends


class ConfigDialog:
    """Porthole's settings window"""

    # Define our response IDs (positive ints → GTK4-friendly)
    # keeping gtk3 values for now
    RESPONSE_HELP  = -11
    RESPONSE_APPLY = -10
    RESPONSE_CANCEL = -6 # Gtk.ResponseType.CANCEL still -6, ok for now
    RESPONSE_OK = -5     # Gtk.ResponseType.OK     still -5

    def __init__(self, page=None):
        """ Initialize Config GUI; page names a tab to open at ("updates") """

        # Parse glade file
        self.gladefile = config.Prefs.DATA_PATH + "/glade/config.ui"
        self.builder = Gtk.Builder()
        self.builder.add_from_file(self.gladefile)
        self.builder.set_translation_domain(config.Prefs.APP)

        self.window = self.builder.get_object("config")
        self.window.set_title(_("Porthole: settings"))
        self.KeywordsFrame = self.builder.get_object("archlist_frame")

        # register callbacks
        callbacks = {
            "on_color_set" : self.on_color_set,
            "on_color_clicked" : self.on_color_clicked,
            "on_enable_archlist_toggled" : self.toggle_archlist,
        }
        self.builder.connect_signals(callbacks)

        debug.dprint("CONFIGDIALOG: connecting buttons to callback()")
        # Connect the dialog's response signal once
        self.window.connect("response", self.on_config_response)
        self.window.set_default_response(self.RESPONSE_OK)

        # Hide widgets which haven't been implemented yet
        self.hide_widgets()

        # build list of widgets and equivalent prefs
        self.build_widget_lists()

        # set widget values to values in prefs
        self.set_widget_values()

        # three pages: Porthole (your own preferences), System and Updates
        # (the computer's, saved as root)
        notebook = self.builder.get_object("primary_nb")
        self.regroup_porthole_page(notebook)
        from porthole.updater.settings import SystemSettingsPage, UpdaterSettingsPage
        self.system_page = SystemSettingsPage()
        self.updates_page = UpdaterSettingsPage()
        indexes = {}
        for name, title, widget in (("system", _("System"), self.system_page),
                                    ("updates", _("Updates"), self.updates_page)):
            scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroller.add(widget)
            indexes[name] = notebook.append_page(scroller, Gtk.Label(label=title))

        self.restore_size()
        self.window.show_all()
        if page in indexes:
            notebook.set_current_page(indexes[page])

    #-----------------------------------------------
    # GUI Callback function definitions start here
    #-----------------------------------------------

    def restore_size(self):
        """Open at the size it was left at (at first: big enough to use),
        never bigger than the screen"""
        try:
            width, height = int(config.Prefs.settings.width), int(config.Prefs.settings.height)
        except (AttributeError, TypeError, ValueError):
            width, height = 860, 660
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0) if display else None
        if monitor:
            area = monitor.get_workarea()
            width, height = min(width, area.width - 40), min(height, area.height - 40)
        self.window.set_default_size(max(width, 600), max(height, 400))

        def remember(window, event):
            config.Prefs.settings.width, config.Prefs.settings.height = window.get_size()
            return False
        self.window.connect("configure-event", remember)

    def regroup_porthole_page(self, notebook):
        """The old Global, Main Window, Emerge and Terminal pages become
        tabs of one Porthole page: your own preferences, apart from the
        computer's settings. The widgets are the same ones (their saving
        code is unchanged), only moved."""
        old = [notebook.get_nth_page(i) for i in range(notebook.get_n_pages())]
        globals_page, mainwindow_nb, emerge_page, terminal_nb = old[:4]
        for page in old[:4]:
            notebook.remove_page(notebook.page_num(page))
        main_pages = [mainwindow_nb.get_nth_page(i) for i in range(3)]
        for page in main_pages:
            mainwindow_nb.remove_page(mainwindow_nb.page_num(page))
        colours_page, summary_page, view_page = main_pages

        tabs = Gtk.Notebook()
        tabs.set_border_width(6)

        def tab(title, *widgets):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=12,
                          valign=Gtk.Align.START)
            for widget in widgets:
                # the old pages asked to fill their notebook tab: stack them instead
                widget.set_vexpand(False)
                widget.set_valign(Gtk.Align.START)
                box.pack_start(widget, False, False, 0)
            scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroller.add(box)
            tabs.append_page(scroller, Gtk.Label(label=title))

        # only the update/downgrade colours are used; the rest never were
        for name in ("normal_fg", "normal_bg", "use_plus_fg", "use_minus_fg",
                     "label18", "label39", "label47", "label48"):
            widget = self.builder.get_object(name)
            widget.set_no_show_all(True)
            widget.hide()
        self.builder.get_object("label19").set_markup(
            _("<b>Colours in the package list</b>"))
        self.builder.get_object("label16").set_label(_("Packages with an update"))
        self.builder.get_object("label17").set_label(_("Packages Portage would downgrade"))
        view_page.get_children()[0].set_markup(_("<b>Columns in the package list</b>"))
        self.builder.get_object("label14").set_markup(
            _("<b>Shown in the package details</b> (Summary tab)"))
        for label in (view_page.get_children()[0], self.builder.get_object("label19"),
                      self.builder.get_object("label14")):
            label.set_xalign(0)
        tab(_("General"), self.unlock_setting())
        colours_page.pack_start(self.match_theme_check(), False, False, 0)
        colours_page.reorder_child(self.match_theme, 2)
        tab(_("Package list"), view_page, colours_page)
        # the arch list chooses the keyword columns of the version table
        viewport = self.builder.get_object("viewport1")
        arch_grid = viewport.get_child()
        viewport.remove(arch_grid)
        self.builder.get_object("globals_enable_archlist").set_label(
            _("Show keyword columns for other architectures"))
        self.builder.get_object("label6").set_markup(
            _("<b>Architectures</b> shown in the version table"))
        tab(_("Package details"), summary_page, arch_grid)
        terminal_tab_widgets = [terminal_nb]
        # emerge output options and saved custom commands; the old warning
        # about the advanced emerge dialog has no option left to warn about
        frames = emerge_page.get_children()
        for frame in frames:
            emerge_page.remove(frame)
        frames[1].destroy()
        frames[0].set_label(_("Emerge output"))
        frames[2].set_label(_("Saved commands"))
        self.builder.get_object("emerge_verbose").set_label(_("Verbose (more detail)"))
        self.builder.get_object("emerge_nospinner").set_label(
            _("No spinner (quieter output)"))
        self.builder.get_object("label57").set_label(
            _("Offered in Porthole, Run Custom Command. Any command can be run there; "
              "it runs in Porthole's terminal window."))
        self.builder.get_object("label57").set_line_wrap(True)
        # how emerge's output looks belongs with the terminal showing it
        tab(_("Terminal"), *terminal_tab_widgets, frames[0])
        tab(_("Commands"), frames[2])
        globals_page.destroy()
        notebook.insert_page(tabs, Gtk.Label(label=_("Porthole")), 0)

    def match_theme_check(self):
        """Package-list colours from the theme (until the user picks some)"""
        self.match_theme = Gtk.CheckButton(label=_("Match the theme (colours are worked "
                                                   "out to read well on it)"))
        self.match_theme.set_active(not config.Prefs.views.upgradable_fg
                                    and not config.Prefs.views.downgradable_fg)

        def toggled(button):
            if not button.get_active():
                return
            from porthole.utils.themecolours import auto
            for name in ("upgradable_fg", "downgradable_fg"):
                self.builder.get_object(name).set_color(Gdk.color_parse(
                    auto("upgrade" if name == "upgradable_fg" else "downgrade")))
        self.match_theme.connect("toggled", toggled)
        toggled(self.match_theme)
        # picking a colour yourself means you chose it
        for name in ("upgradable_fg", "downgradable_fg"):
            self.builder.get_object(name).connect(
                "color-set", lambda w: self.match_theme.set_active(False))
        return self.match_theme

    def unlock_setting(self):
        """How long Unlock lasts: the user's own Porthole setting"""
        from porthole.updater.session import TIMEOUTS, unlock_minutes
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>%s</b>" % _("Unlocking"))
        box.pack_start(heading, False, False, 0)
        row = Gtk.Box(spacing=12)
        row.pack_start(Gtk.Label(label=_("Unlock Porthole for"), xalign=0), False, False, 0)
        self.unlock_time = Gtk.ComboBoxText()
        for minutes, label in TIMEOUTS:
            self.unlock_time.append(str(minutes), _(label))
        self.unlock_time.set_active_id(str(unlock_minutes()))
        row.pack_start(self.unlock_time, False, False, 0)
        box.pack_start(row, False, False, 0)
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        hint.set_markup("<small>%s</small>" % _(
            "Unlock (on the toolbar) asks for your password once; until this time "
            "is up, changes need no password. Used from the next unlock."))
        box.pack_start(hint, False, False, 0)
        return box

    def on_config_response(self, dialog_widget, response_id):
        """ Parse dialog response (ok, cancel, apply or help clicked) """
        debug.dprint("CONFIGDIALOG: on_config_response(): response_id '%s'" % response_id)
        if response_id == self.RESPONSE_OK:
            debug.dprint("CONFIGDIALOG: on_config_response(): OK response_id '%s'" % response_id)
            self.apply_widget_values()
            self.save_computer_settings(close=True)
            self.window.hide()
        elif response_id == self.RESPONSE_CANCEL:
            debug.dprint("CONFIGDIALOG: on_config_response(): Cancel response_id '%s'" % response_id)
            self.window.destroy()
        elif response_id == self.RESPONSE_APPLY:
            debug.dprint("CONFIGDIALOG: on_config_response(): Apply response_id '%s'" % response_id)
            self.apply_widget_values()
            self.save_computer_settings(close=False)
        elif response_id == self.RESPONSE_HELP:
            debug.dprint("CONFIGDIALOG: on_config_response(): Help response_id '%s'" % response_id)
            #Display help file with web browser
            load_web_page('file://' + config.Prefs.DATA_PATH + 'help/customize.html')
        else:
            debug.dprint(f"CONFIGDIALOG: Unhandled response: {response_id}")

    def on_color_clicked(self, button_widget, event):
        """ Make sure colour dialog doesn't show alpha choice """
        debug.dprint("CONFIGDIALOG: on_color_clicked()")
        if event.button == 1: # primary mouse button
            button_widget.set_use_alpha(False)
            return False # continue to build colour selection dialog
        if event.button == 3: # secondary mouse button
            if button_widget.get_alpha() > 40000:
                button_widget.set_alpha(32767)
                button_widget.set_use_alpha(True)
                name = button_widget.get_property('name')
                if name == 'default_fg':
                    button_widget.set_color(self.default_textview_fg)
                elif name == 'default_bg':
                    button_widget.set_color(self.default_textview_bg)
                else:
                    ext = name[-3:]
                    color = self.builder.get_object('default' + ext).get_color()
                    button_widget.set_color(color)
                self.on_color_set(button_widget)
                return True
            else:
                button_widget.set_alpha(65535)
                button_widget.set_use_alpha(False)
                self.on_color_set(button_widget)
                return True

    def on_color_set(self, button_widget):
        debug.dprint("CONFIGDIALOG: on_color_set()")
        # if button is default-button: change colour in all buttons using default.
        color = button_widget.get_color()
        ext = None
        if button_widget.get_property('name') == 'default_fg':
            ext = "fg"
        elif button_widget.get_property('name') == 'default_bg':
            ext = "bg"
        if not ext or not hasattr(self, 'tagnamelist'):
            return
        for name in self.tagnamelist:
            widget = self.builder.get_object('_'.join([name, ext]))
            if widget:
                if widget.get_alpha() < 40000:
                    widget.set_color(color)

    #------------------------------------------
    # Support function definitions start here
    #------------------------------------------

    def save_computer_settings(self, close):
        """System, then Updates (each as root, only if changed)"""
        def system_saved(ok, message):
            if not ok:
                dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.CLOSE,
                                           text=_("The system settings weren't saved"))
                dialog.format_secondary_text(message or _("Cancelled."))
                dialog.run()
                dialog.destroy()
            self.updates_page.save(lambda ok, msg: self.updates_saved(ok, msg, close=close))
        self.system_page.save(system_saved)

    def updates_saved(self, ok, message, close=True):
        """After saving the update settings (as root, so it may be refused)"""
        if not ok:
            dialog = Gtk.MessageDialog(transient_for=self.window if not close else None,
                                       modal=True, message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE,
                                       text=_("The update settings weren't saved"))
            dialog.format_secondary_text(message or _("Cancelled."))
            dialog.run()
            dialog.destroy()
            if close:
                self.window.show()  # let them try again
                return
        if close:
            self.window.destroy()

    def hide_widgets(self):
        # hide unimplemented widgets
        hidelist = [ # widget names
            'label_custom_editor',
            'custom_editor_command',
            'main_font_box',
            'sample_scrolled_window_1',
            'sample_label_1',
            'sample_scrolled_window_2',
            'sample_label_2',
        ]
        for name in hidelist:
            widget = self.builder.get_object(name)
            if widget:
                # show_all() later must not bring it back
                widget.set_no_show_all(True)
                widget.hide()
        # hide unimplemented notebook tabs
        # removelist = [ # [notebook, tab to remove]
        #     ['terminal_notebook', 'filter_tab'],
        #     ['main_window_notebook', 'category_view_tab'],
        #     ['main_window_notebook', 'package_view_tab'],
        # ]
        # for notebook, tab in removelist:
        #     notewidget = self.builder.get_object(notebook)
        #     tabwidget = self.builder.get_object(tab)
        #     tabnum = notewidget.page_num(tabwidget)
        #     notewidget.remove_page(tabnum)
        return

    def build_widget_lists(self):
        """ Build lists of widgets and equivalent prefs """
        self.tagnamelist = ['command', 'emerge', 'error', 'info', 'caution',
            'warning', 'note', 'linenumber', 'default']
        self.xtermtaglist = ['black', 'red', 'green', 'yellow', 'blue',
            'magenta', 'cyan', 'white']
        self.viewoptions = ['downgradable_fg', 'upgradable_fg', 'normal_fg', 'normal_bg']
        self.checkboxes = [
            ['summary', 'showtable'],
            ['summary', 'showkeywords'],
            ['summary', 'showavailable'],
            ['summary', 'showinstalled'],
            ['summary', 'showlongdesc'],
            ['summary', 'showuseflags'],
            ['summary', 'showlicense'],
            ['summary', 'showurl'],
            ['emerge', 'verbose'],
            ['emerge', 'nospinner'],
            ['globals', 'enable_archlist'],
            ##['globals', 'enable_all_keywords'],
            ['views', 'show_installed'],
            ['views', 'show_dl'],
            ['views', 'show_recommended'],
        ]

    def set_widget_values(self):
        """ Set widget attributes based on prefs """

        # Display current CPU architecture
        widget = self.builder.get_object('current_arch')
        widget.set_label(config.Prefs.myarch)

        # Terminal Color Tags
        default = config.Prefs.TAG_DICT['default']
        # what the terminal really shows where no colour is set: the theme's
        from porthole.utils.themecolours import hex_of, palette
        theme = palette()
        self.default_textview_fg = Gdk.color_parse(hex_of(theme["text"]))
        self.default_textview_bg = Gdk.color_parse(hex_of(theme["base"]))
        if default[0]: default_fg = Gdk.color_parse(default[0])
        else: default_fg = self.default_textview_fg
        if default[1]: default_bg = Gdk.color_parse(default[1])
        else: default_bg = self.default_textview_bg

        for name in self.tagnamelist:
            color = config.Prefs.TAG_DICT[name][0] # fg
            widget = self.builder.get_object(name + '_fg')
            if widget:
                if color:
                    widget.set_color(Gdk.color_parse(color))
                else:
                    widget.set_color(default_fg)
                    widget.set_alpha(32767) # to show it's using the default value
                    widget.set_use_alpha(True)
            color = config.Prefs.TAG_DICT[name][1] # bg
            widget = self.builder.get_object(name + '_bg')
            if widget:
                if color:
                    widget.set_color(Gdk.color_parse(color))
                else:
                    widget.set_color(default_bg)
                    widget.set_alpha(32767) # to show it's using the default value
                    widget.set_use_alpha(True)

        # Terminal Font:
        widget = self.builder.get_object('terminal_font')
        if widget:
            if config.Prefs.terminal.font:
                widget.set_font_name(config.Prefs.terminal.font)
            self.terminal_font = widget.get_font_name()

        # Default XTerm colours:
        for name in self.xtermtaglist:
            color = config.Prefs.TAG_DICT['fg_' + name][0]
            widget = self.builder.get_object('fg_' + name)
            if widget:
                if color:
                    widget.set_color(Gdk.color_parse(color))
                else: # this should never happen, but just in case...
                    widget.set_color(Gdk.color_parse(name))
            color = config.Prefs.TAG_DICT['bg_' + name][1]
            widget = self.builder.get_object('bg_' + name)
            if widget:
                if color:
                    widget.set_color(Gdk.color_parse(color))
                else: # this should never happen, but just in case...
                    widget.set_color(Gdk.color_parse(name))

        # View Colours
        for name in self.viewoptions:
            color = getattr(config.Prefs.views, name)
            widget = self.builder.get_object(name)
            if widget:
                if color:
                    widget.set_color(Gdk.color_parse(color))
                else:
                    widget.set_color(default_fg)

        # Checkboxes:
        for category, name in self.checkboxes:
            widget = self.builder.get_object('_'.join([category, name]))
            debug.dprint("CONFIGDIALOG: set_widget_values(); Checkboxes: widget = %s" %('_'.join([category, name])))
            if widget:
                active = getattr(getattr(config.Prefs, category), name)
                debug.dprint(str(active))
                if active == []:
                    active = False
                widget.set_active(active)
            else:
                debug.dprint("CONFIGDIALOG: set_widget_values(); Checkboxes: widget = %s not found!" %('_'.join([category, name])))

        # Custom Command History
        for x in [1, 2, 3, 4, 5]:
            widget = self.builder.get_object('history' + str(x))
            if widget:
                pref = config.Prefs.run_dialog.history[x]
                widget.set_text(pref)

        # build the arch list widget
        self.build_archlist_widget()


    def apply_widget_values(self):
        """ Set prefs from widget values """
        config.Prefs.globals.unlock_minutes = int(self.unlock_time.get_active_id())
        # Terminal Color Tags
        for name in self.tagnamelist:
            widget = self.builder.get_object(name + '_fg')
            if widget:
                color = widget.get_color()
                alpha = widget.get_alpha()
                #debug.dprint("CONFIGDIALOG: '%s_fg': previous value: '%s'" % (name, config.Prefs.TAG_DICT[name][0]))
                if alpha > 40000: # colour set
                    #debug.dprint("CONFIGDIALOG: setting to '%s'" % self.get_color_spec(color))
                    config.Prefs.TAG_DICT[name][0] = self.get_color_spec(color)
                else: # use default
                    #debug.dprint("CONFIGDIALOG: setting to ''")
                    config.Prefs.TAG_DICT[name][0] = ''
            widget = self.builder.get_object(name + '_bg')
            if widget:
                color = widget.get_color()
                alpha = widget.get_alpha()
                #debug.dprint("CONFIGDIALOG: '%s_bg': previous value: '%s'" % (name, config.Prefs.TAG_DICT[name][1]))
                if alpha > 40000:
                    #debug.dprint("CONFIGDIALOG: setting to '%s'" % self.get_color_spec(color))
                    config.Prefs.TAG_DICT[name][1] = self.get_color_spec(color)
                else:
                    #debug.dprint("CONFIGDIALOG: setting to ''")
                    config.Prefs.TAG_DICT[name][1] = ''

        # Terminal Font:
        widget = self.builder.get_object('terminal_font')
        if widget and self.terminal_font != widget.get_font_name():
            config.Prefs.terminal.font = widget.get_font_name()

        # Default XTerm colours:
        for name in self.xtermtaglist:
            widget = self.builder.get_object('fg_' + name)
            if widget:
                color = widget.get_color()
                if color:
                    config.Prefs.TAG_DICT['fg_' + name][0] = self.get_color_spec(color)
            widget = self.builder.get_object('bg_' + name)
            if widget:
                color = widget.get_color()
                if color:
                    config.Prefs.TAG_DICT['bg_' + name][1] = self.get_color_spec(color)
                else: # this should never happen, but just in case...
                    widget.set_color(Gdk.color_parse(name))

        # View Colours
        for name in self.viewoptions:
            widget = self.builder.get_object(name)
            if widget:
                color = widget.get_color()
                alpha = widget.get_alpha()
                if alpha:
                    setattr(config.Prefs.views, name, self.get_color_spec(color))
                else:
                    setattr(config.Prefs.views, name, '')
        if self.match_theme.get_active():
            # from the theme, whatever it is next time: nothing chosen
            config.Prefs.views.upgradable_fg = config.Prefs.views.downgradable_fg = ""

        # Checkboxes:
        for category, name in self.checkboxes:
            debug.dprint("CONFIGDIALOG: apply_widget_values(); name = %s" %name)
            widget = self.builder.get_object('_'.join([category, name]))
            if widget:
                debug.dprint("CONFIGDIALOG: apply_widget_values(); name = %s widget found" %name)
                active = widget.get_active()
                setattr(getattr(config.Prefs, category), name, active)
                if name == 'enable_archlist' and active:
                    archlist = []
                    keyword = ''
                    for item in self.kwList:
                        keyword = item[1]
                        if item[0].get_active():
                            debug.dprint(item[1])
                            archlist.append(keyword)
                    debug.dprint("CONFIGDIALOG: new archlist = %s" %str(archlist))
                    config.Prefs.globals.archlist = archlist[:]


        # Custom Command History
        for x in [1, 2, 3, 4, 5]:
            widget = self.builder.get_object('history' + str(x))
            if widget:
                text = widget.get_text()
                if text != config.Prefs.run_dialog.history[x]:
                    config.Prefs.run_dialog.history[x] = text

    def get_color_spec(self, color):
        red = hex(color.red)[2:].zfill(4)
        green = hex(color.green)[2:].zfill(4)
        blue = hex(color.blue)[2:].zfill(4)
        return '#' + red + green + blue

    def build_archlist_widget(self):
        """ Create a table layout and populate it with
            checkbox widgets representing the available
            keywords
        """
        debug.dprint("CONFIGDIALOG: build_archlist_widget()")

        # If frame has any children, remove them
        child = self.KeywordsFrame.get_child()
        if child != None:
            self.KeywordsFrame.remove(child)

        keywords = backends.portage_lib.get_archlist()

        # Build table to hold radiobuttons
        size = len(keywords)
        maxcol = 3
        maxrow = size / maxcol - 1
        if maxrow < 1:
            maxrow = 1
        table = Gtk.Table(maxrow, maxcol-1, True)
        self.KeywordsFrame.add(table)
        self.kwList = []

        # Iterate through use flags collection, create
        # checkboxes and attach to table
        col = 0
        row = 0
        button_added = False
        clickable_button = False
        for keyword in keywords:
            button = Gtk.CheckButton(keyword)
            self.kwList.append([button, keyword])
            table.attach(button, col, col+1, row, row+1)
            button.show()
            button_added = True
            clickable_button = True
            button.set_active(keyword in config.Prefs.globals.archlist)
            # Increment col & row counters
            if button_added:
                col += 1
                if col > maxcol:
                    col = 0
                    row += 1
        if clickable_button:
            # Display the entire table
            table.show()
            self.KeywordsFrame.show()
            self.KeywordsFrame.set_sensitive(config.Prefs.globals.enable_archlist)
        else:
            self.KeywordsFrame.set_sensitive(False)
        debug.dprint("CONFIGDIALOG: build_archlist_widget(); widget build completed")

    def toggle_archlist(self, widget):
        """Toggles the archlist frame sensitivity
        """
        debug.dprint("CONFIGDIALOG: toggle_archlist(); signal caught")
        self.KeywordsFrame.set_sensitive(widget.get_active())
        config.Prefs.globals.enable_archlist = widget.get_active()

