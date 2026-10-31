#!/usr/bin/env python3

from glob import glob

from setuptools import setup

datadir = "share/porthole/"

setup( \
    name = "porthole",
    # the git description shown in the about box isn't a valid package
    # version; packages set porthole/version.py themselves
    version = "0.7.0.dev0",
    description = "GTK+ frontend to Portage",
    author = "Fredrik Arnerup, Daniel G. Taylor, Brian Dolbec, William F. Wheeler",
    author_email = "dol-sen@users.sourceforge.net, " \
                    "farnerup@users.sourceforge.net, dgt84@users.sourceforge.net, " \
                    " tiredoldcoder@users.sourceforge.net",
    url = "http://porthole.sourceforge.net",
    packages = ['porthole',  'porthole.advancedemerge', 'porthole.backends', 'porthole.config',
                        'porthole.db', 'porthole.dialogs', 'porthole.loaders', 'porthole.mwsupport',
                        'porthole.plugins', 'porthole.readers', 'porthole.terminal',
                        'porthole.updater', 'porthole.utils', 'porthole.views', 'porthole._xml',
                        'porthole.views.packagebook'],
    package_dir = {'porthole':'porthole'},
    scripts = ["scripts/porthole", "scripts/porthole-update-check",
               "scripts/porthole-update-service", "scripts/porthole-update-notifier",
               "scripts/porthole-askpass"],
    data_files = [
        (datadir + "pixmaps",
            ["porthole/pixmaps/porthole-about.png", "porthole/pixmaps/porthole-icon.png", 
            "porthole/pixmaps/porthole-clock-20x20.png", "porthole/pixmaps/porthole-clock.png",
            "porthole/pixmaps/porthole.svg"]),
        # every help page, its stylesheet and screenshots
        (datadir + "help", sorted(glob("porthole/help/*.html") + glob("porthole/help/*.css")
                                  + glob("porthole/help/*.png"))),
        (datadir + "glade", sorted(glob("porthole/glade/*.ui"))),
        (datadir,
            ["scripts/dopot.sh", "scripts/pocompile.sh", "AUTHORS"]),
        (datadir + "config",
            [ "porthole/config/configuration.xml"]),
        (datadir + "i18n",
            ["porthole/i18n/messages.pot", "porthole/i18n/TRANSLATING"]),
        ("share/applications", ["porthole.desktop"]),
        # data/updater.conf, data/porthole-update.cron and
        # data/porthole-update-notifier.desktop go under /etc, which a
        # wheel can't install to; the package installs them
        ("share/polkit-1/actions", ["data/org.porthole.policy"]),
        ("share/pixmaps", ["porthole/pixmaps/porthole-icon.png"])
    ]
)
