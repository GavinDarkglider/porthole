#!/usr/bin/env python

'''
    Porthole Mainwindow Action support
    Support class and functions for the mainwindow interface

    Copyright (C) 2003 - 2011
    Fredrik Arnerup, Brian Dolbec,
    Daniel G. Taylor, Wm. F. Wheeler, Tommy Iorns

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

from gettext import gettext as _

from porthole import config
from porthole import db

from porthole.utils import (
    debug,
    utils
)
from porthole import backends
PMS_LIB = backends.portage_lib
from porthole.advancedemerge.advemerge import AdvancedEmergeDialog
from porthole.dialogs.simple import SingleButtonDialog
from porthole.dialogs.command import RunDialog
from porthole.terminal.terminal import ProcessManager
from porthole.backends.utilities import get_sync_info

from porthole.mwsupport.category import CategoryHandler


class ActionHandler(CategoryHandler):
    '''Support functions for mainwindow actions'''

    def __init__(self):
        CategoryHandler.__init__(self)
        self.get_sync_time()
        self.new_sync = False
        # create and start our process manager
        self.process_manager = ProcessManager(utils.environment(), False)

    def package_update(self, pkg):
        """callback function to update an individual package
            after a successfull emerge was detected"""
        # find the pkg in db.db
        db.db.update(PMS_LIB.extract_package(pkg))

    def submit_to_runner(self, package_name, command):
        """Installs and removals run in the root job runner, so closing
        Porthole never stops them; pretend runs stay here, as the user.
        Returns True if the command was taken."""
        from porthole.updater.commands import install_targets, runner_tokens, sync_command
        if sync_command(command) and not config.Prefs.emerge.pretend:
            # syncing is the runner's check job: Sync now
            self.sync_tree()
            return True
        tokens = runner_tokens(command, config.Prefs.emerge.pretend)
        if tokens is None:
            return False
        targets = install_targets(tokens)
        if targets is not None:
            # an install: worked out (and its fixes verified) before it runs
            self.request_install(package_name, targets)
            return True
        from porthole.updater.elevate import run_as_root, service
        run_as_root(service("submit", "emerge", "--", *tokens),
                    lambda ok, out: self.runner_submitted(package_name, ok, out))
        return True

    def request_install(self, package_name, targets, requested=()):
        """Emerge, Advanced Emerge and Upgrade: add to the install queue,
        which is planned and installed as a whole"""
        self.install_queue.add(package_name, targets, requested)
        from porthole.updater.installqueue import added_popup
        added_popup(self.mainwindow, package_name, len(self.install_queue.items),
                    self.show_install_queue)

    def show_install_queue(self, *widget):
        from porthole.updater.installqueue import InstallQueueWindow
        if self.install_queue_window is None:
            self.install_queue_window = InstallQueueWindow(self.install_queue,
                                                           self.plan_install)
        self.install_queue_window.present()

    def plan_install(self, package_name, targets, requested, on_queued=None, world=()):
        """Work out the queue as the user (no password) and show the plan
        for review; only Install there asks for root. on_queued() runs once
        its job is queued. world: of the --oneshot targets, the packages
        still meant for the world file (see InstallQueue.combined)."""
        from porthole.updater.elevate import plan_as_user
        from porthole.updater.review import working
        busy = working(self.mainwindow, _("Working out what installing %s needs...")
                       % package_name)
        request = {"targets": list(targets), "requested": list(requested), "choices": {}}
        if world:
            request["world"] = list(world)

        def planned(plan_file, error):
            busy.destroy()
            if not plan_file:
                SingleButtonDialog(_("Couldn't plan the install"), self.mainwindow,
                                   error, None, _("OK"))
                self.show_install_queue()  # still there to try again
                return
            from porthole.updater.review import UpdateAssistant
            UpdateAssistant(self.mainwindow, self, plan_file=plan_file,
                            on_queued=on_queued).present_plan()
        plan_as_user(request, planned)

    def install_planned(self, ok, output, on_queued=None):
        if not ok:
            SingleButtonDialog(_("Couldn't plan the install"), self.mainwindow,
                               output or _("Cancelled."), None, _("OK"))
            self.show_install_queue()  # still there to try again
            return
        outcome, _sep, what = output.strip().splitlines()[-1].partition(" ")
        if outcome == "queued":
            if on_queued:
                on_queued()
            from porthole.updater.follow import show_in_terminal
            show_in_terminal(what)
        else:
            from porthole.updater.review import UpdateAssistant
            UpdateAssistant(self.mainwindow, self, plan_file=what,
                            on_queued=on_queued).present_plan()

    def runner_submitted(self, package_name, ok, output):
        """Follow a submitted job in the terminal, or say why it wasn't taken"""
        if not ok:
            SingleButtonDialog(_("Couldn't start"), self.mainwindow,
                               output or _("Cancelled."), None, _("OK"))
            return
        # the job window (one for all jobs); package lists are reloaded
        # when the job ends, see MainWindow.watch_jobs
        from porthole.updater.follow import show_in_terminal
        show_in_terminal(output.strip().splitlines()[-1])

    def get_sync_time(self):
        """gets and returns the timestamp info saved during
           the last portage tree sync"""
        self.last_sync, self.valid_sync = get_sync_info()

    def set_sync_tip(self):
        """Tooltip for the Updates button: the plan, then the last sync"""
        from porthole.updater.analyze import summarize
        from porthole.updater.service import PLAN_FILE, read_json
        plan = read_json(PLAN_FILE)
        title, body = summarize(plan) if plan else (_("No update check has run yet"), "")
        lines = [title] + ([body] if body else [])
        lines.append("%s %s" % (self.sync_tip, self.last_sync.strip()))
        self.widget["btn_sync"].set_has_tooltip(True)
        self.widget["btn_sync"].set_tooltip_text("\n".join(lines))

    def action_callback(self, action = None, arg = None):
        """dispatcher interface callback to handle various actions."""
        debug.dprint("MAINWINDOW: action_callback(); " +
            "caller = %s, action = '%s', arg = %s"
            %(arg['caller'], str(action), str(arg)))
        if action in ["adv_emerge", "change_use", "set path", "package changed", "refresh"]:
            # handle possible spaces in callback action string
            _action = action.replace(' ', '_')
            ret_val = None
            ret_val = getattr(self, "_action_%s_" %_action)(arg)
            if ret_val:
                return ret_val
        else:
            return self._action_install(action, arg)


    def _action_change_use_(self, arg):
        """Change the USE flags of an installed package: Advanced Emerge on
        the installed version, queued as a --oneshot rebuild"""
        package = arg.get('package')
        if package is None:
            return False
        AdvancedEmergeDialog(package, self.setup_command, self.re_init_portage,
                             install=self.request_install, rebuild=True)
        return True

    def _action_adv_emerge_(self, arg):
        """handle advanced emerge action callback"""
        if 'package' in arg:
            package = arg['package']
        elif 'full_name' in arg:
            package = db.db.get_package(arg['full_name'])
        else:
            debug.dprint("MAINWINDOW: _action_adv_emerge_(); did not get an "+
                    "expected arg variable for 'adv_emerge' action arg = " +
                    str(arg))
            return False
        self.adv_emerge_package(package, arg.get('ebuild'))
        return True

    def _action_set_path_(self, arg):
        """handle a path setting callback"""
        # save the path to the package that matched the name passed
        # to populate() in PackageView... (?)
        x = self.widget["view_filter"].get_active()
        self.current_pkg_path[x] = arg['path'] # arg = path

    def _action_package_changed_(self, arg):
        """handle a package changed callback"""
        self.package_changed(arg['package'])
        return True

    def _action_refresh_(self, arg):
        """handle a refresh action callback"""
        self.refresh()
        return True

    def _action_install(self, action, arg):
        """handle install commnad callbacks"""
        old_pretend_value = config.Prefs.emerge.pretend
        old_verbose_value = config.Prefs.emerge.verbose
        if "emerge" in action:
            commands = ["emerge "]
        elif  "unmerge" in action:
            commands = ["emerge --unmerge "]
        if "pretend" in action:
            config.Prefs.emerge.pretend = True
        else:
            config.Prefs.emerge.pretend = False
        commands.append(config.Prefs.emerge.get_string())
        if "oneshot" in action and "--oneshot" not in commands[-1]:
            commands.append("--oneshot ")
        if "ebuild" in arg:
            commands.append('=' + arg['ebuild'])
            cp = PMS_LIB.pkgsplit(arg['ebuild'])[0]
        elif 'package' in arg:
            cp = arg['package'].full_name
            commands.append(arg['package'].full_name)
        elif 'full_name' in arg:
            cp = arg['full_name']
            commands.append(arg['full_name'])
        else:
            debug.dprint("MAINWINDOW action_callback(): unknown arg '%s'"
                    % str(arg))
            return False
        self.setup_command(PMS_LIB.get_name(cp), ''.join(commands))
        config.Prefs.emerge.pretend = old_pretend_value
        config.Prefs.emerge.verbose = old_verbose_value
        return True

    def setup_command(self, package_name, command, run_anyway=False):
        """Installs, removals and syncs go to the root job runner (installs
        by way of the queue); what is left (pretend, help and info runs, and
        custom commands, run_anyway) runs here, as the user"""
        if self.submit_to_runner(package_name, command):
            return True
        is_pretend = config.Prefs.emerge.pretend or utils.pretend_check(command)
        if not (run_anyway or is_pretend or utils.help_check(command)
                or utils.info_check(command)):
            SingleButtonDialog(_("Porthole can't run this"), self.mainwindow,
                               _("'%s' needs root, and the job runner only runs emerge "
                                 "commands that install, update, remove or sync. Run it "
                                 "in a root terminal instead.") % command, None, _("OK"))
            return False
        if is_pretend or utils.help_check(command) or utils.info_check(command):
            callback = lambda: None  # changes nothing
        else:
            callback = self.reload_db  # a custom command may have
        self.process_manager.add(package_name, command, callback,
            _("Porthole Main Window"))
        return True

    def emerge_package(self, package, oneshot=False):
        """Emerge the package (oneshot: not added to the world file)."""
        options = config.Prefs.emerge.get_string()
        if oneshot and "--oneshot" not in options:
            options += "--oneshot "
        self.setup_command(package.get_name(), "emerge" + options + package.full_name)

    def adv_emerge_package(self, package, ebuild=None):
        """Advanced emerge of the package (starting at version ebuild)."""
        # Activate the advanced emerge dialog window
        # re_init_portage callback is for when package.use etc. are modified
        return AdvancedEmergeDialog(package, self.setup_command,
                self.re_init_portage, install=self.request_install, ebuild=ebuild)

    def unmerge_package(self, package, ebuild=None):
        """Unmerge the package, or just one installed version of it."""
        target = "=" + ebuild if ebuild else package.full_name
        self.setup_command(package.get_name(), "emerge --unmerge" +
                config.Prefs.emerge.get_string() + target)

    def sync_tree(self, *widget):
        """Sync the repositories (and check for updates) as a runner job"""
        from porthole.updater.elevate import sync_now
        from porthole.updater.follow import show_in_terminal
        sync_now(self.mainwindow, show_in_terminal)

    def open_log(self, widget):
        """The logs of the job runner's jobs (and of saved logs)"""
        from porthole.updater.joblogs import JobLogsWindow
        JobLogsWindow(self.mainwindow)

    def custom_run(self, *widget):
        """ Run a custom command in the terminal window """
        #debug.dprint("MAINWINDOW: entering custom_run")
        #debug.dprint(config.Prefs.run_dialog.history)
        RunDialog(self.setup_command, run_anyway=True)
        return

