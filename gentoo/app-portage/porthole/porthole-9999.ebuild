# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( python3_{12..14} )
DISTUTILS_USE_PEP517=setuptools
DISTUTILS_SINGLE_IMPL=1

inherit desktop distutils-r1 git-r3 optfeature xdg

DESCRIPTION="GTK frontend to Portage with an update assistant"
HOMEPAGE="https://github.com/GavinDarkglider/porthole https://porthole.sourceforge.net"
EGIT_REPO_URI="https://github.com/GavinDarkglider/porthole.git"
EGIT_BRANCH="once-over"

LICENSE="GPL-2+"
SLOT="0"
IUSE="crossdev +notifier"

# diffutils: diff3 merges configuration file updates
RDEPEND="
	$(python_gen_cond_dep '
		dev-python/pygobject:3[${PYTHON_USEDEP}]
		sys-apps/portage[${PYTHON_USEDEP}]
	')
	sys-apps/diffutils
	sys-auth/polkit
	x11-libs/gtk+:3[introspection]
	crossdev? ( sys-devel/crossdev )
	notifier? (
		dev-libs/libayatana-appindicator
		virtual/cron
		x11-libs/libXScrnSaver
		x11-libs/libnotify[introspection]
	)
"

src_prepare() {
	distutils-r1_src_prepare
	# no git checkout to ask once installed: record the commit built
	sed -i 's/^version ="git-"$/version = "9999-once-over-'"${EGIT_VERSION:0:10}"'"/' \
		porthole/version.py || die
	grep -q '^version = "9999-once-over-' porthole/version.py || die "version not set"
}

python_install_all() {
	distutils-r1_python_install_all

	# Every setting with its default is in /usr/share/porthole/updater.conf
	# (setup.py installs it); /etc/porthole/updater.conf only holds what was
	# changed, written by Porthole, so it never comes with the package
	if use notifier; then
		exeinto /etc/cron.hourly
		newexe data/porthole-update.cron porthole-update
		insinto /etc/xdg/autostart
		doins data/porthole-update-notifier.desktop
	else
		rm "${ED}"/usr/bin/porthole-update-notifier || die
	fi
	# plugins come from the same tree; keep the ones asked for
	if ! use crossdev; then
		rm -r "${D}$(python_get_sitedir)"/porthole/plugins/crossdev || die
	fi
	keepdir /etc/porthole /var/lib/porthole /var/log/porthole
}

pkg_postinst() {
	xdg_pkg_postinst
	if use notifier; then
		elog "The update service runs hourly from /etc/cron.hourly and syncs and"
		elog "checks for updates on the schedule set in Porthole: Settings, Updates"
		elog "(saved in /etc/porthole/updater.conf; every setting and its default:"
		elog "/usr/share/porthole/updater.conf). Porthole asks on its first start how"
		elog "updates should run: tell you, build approved ones in a build window,"
		elog "or fully automatic."
		elog ""
		elog "The tray notifier starts with your next desktop login, or run:"
		elog "  porthole-update-notifier &"
		elog "It also tells the update service when the computer is in use, so"
		elog "builds in the build window pause while someone works."
	fi
	if [[ -n $(find "${EROOT}"/etc/porthole -maxdepth 1 -name '._cfg????_updater.conf' \
			2>/dev/null) ]]; then
		elog ""
		elog "An earlier Porthole left a new updater.conf waiting next to yours."
		elog "Porthole: Actions, Configuration Updates shows it with your settings"
		elog "kept; or remove /etc/porthole/._cfg*_updater.conf, as your file now"
		elog "only needs the settings you changed."
	fi
	optfeature "rebuilding live (-9999) packages with new upstream commits" \
		app-portage/smart-live-rebuild
	if use crossdev; then
		elog "Switch the crossdev plugin on in Porthole: Settings, Plugins."
		optfeature "building what won't cross-compile, in an emulator" \
			"app-emulation/qemu[static-user]"
		optfeature "compiling emulated builds natively" sys-devel/distcc
	fi
}
