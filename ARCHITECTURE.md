# Porthole: how the once-over branch is put together

This is for Porthole's maintainers and anyone reviewing the `once-over`
branch. The user-facing description is the in-app help (`porthole/help/`).

## What changed, in one paragraph

Porthole still browses and searches packages as before, but it no longer
runs emerge itself as root. Everything that changes the system goes through
one checked root entry point, `porthole-update-service`, to a job runner
that keeps going when windows close. Installs and updates are planned first
by a resolver that works out the setting changes Portage needs, verifies
every one with Portage, and explains what it can't do; the user reviews the
plan, and root re-plans and only proceeds if the result is the plan that was
approved. A scheduled service syncs and checks for updates, a tray applet
tells the user, and the user decides how much may happen by itself. The
settings window covers the system's Portage setup without rewriting
`make.conf`.

## The four parts

| Part | Code | Runs as |
|---|---|---|
| Front end (GTK) | `porthole/` (main window, views, dialogs), `porthole/updater/review.py` (plan review), `settings.py`, `repospage.py`, `binpkgpage.py` | user |
| Resolver | `porthole/updater/analyze.py` (plans), `resolve.py` (Portage depgraph worker), `autopilot.py` (decisions for fully automatic updates) | user, and root to re-check |
| Runner | `porthole/updater/runner.py` (jobs, one at a time, in a pty), `window.py` (build window, pausing), `ondemand.py` (services a job needs only while it runs: its own distccd on 127.0.0.1, qemu-binfmt) | root |
| Repositories for other systems | `porthole/updater/repopool.py`: Porthole's own list (`/etc/porthole/repos.conf`) of repositories only the systems built for other computers use, synced with emaint given only that list (`PORTAGE_REPOSITORIES`); this computer's repos.conf never sees them | root |
| Crossdev plugin | `porthole/plugins/crossdev/`: `roots.py` (systems for other computers, each a root and target), `toolchain.py` (the cross toolchain its profile allows), `conf.py` (its settings: Porthole's block in the system's own make.conf, starting from the profile's values, and its other Portage files), `window.py` / `settingswin.py` (front end), `root.py` (root commands and job steps) | user / root |
| Tray applet | `porthole/updater/notifier.py`, `scripts/porthole-update-notifier` | user |

They meet in `porthole/updater/service.py` (`scripts/porthole-update-service`):
the scheduled check (`run`, from `/etc/cron.hourly/porthole-update`) and
every root action the front end asks for.

## Privilege model

* Nothing needs a password to look: browsing, searching, planning and
  checking for updates run as the user.
* Changes run `porthole-update-service` as root through `sudo -A`
  (`scripts/porthole-askpass` asks for the password), falling back to
  `pkexec` if sudo refuses (`elevate.py`). sudoers decides; NOPASSWD rules
  work as expected.
* **Unlock** (`session.py`) starts `porthole-update-service session` as a
  child of Porthole, talking JSON lines over its stdin/stdout only (no
  socket). It runs only the service's verbs, and stops on EOF, lock, or
  timeout. While unlocked Porthole is non-dumpable (`PR_SET_DUMPABLE 0`), so
  other processes of the same user can't ptrace it or open the pipe through
  `/proc`. The helper refuses `submit command`: custom commands as root
  always go through `sudo -k`.
* Every verb checks its input as root: emerge command lines against an
  allowlist (`runner.check_emerge`, no shell), install requests
  (`check_request`), choices, settings, names and addresses. The legacy
  config editor (`backends/set_config.py`) only edits the `package.*` files
  of a known system and never `make.conf`.
* A plan made as the user is only a fingerprint for root:
  `analyze.plan_signature`; the runner's `plan-install` step re-plans as root
  and stops if it differs.
* Running from a user-writable checkout shows a warning bar
  (`commands.unsafe_location`), since root would run that code.

## The service's verbs

```
run [--force] [--no-sync]        the scheduled sync and check
submit update --plan TIME        queue a reviewed update plan
submit check [--sync-now]        queue a sync and check
submit emerge -- CMD...          queue a checked emerge (removals, rebuilds)
submit install --request J [--expect SIG] / --plan F --generated T
submit command -- LINE           a custom command as root (confirmed by the user)
  ... --later                    wait for the build window
cancel [JOB] [--drop]            stop jobs
keep-building [JOB]              don't pause this build
config [KEY=VALUE...]            /etc/porthole/updater.conf
plan --request J                 plan as root, JSON on stdout
sets NAME                        root-only sets (preserved-rebuild)
news read|unread --repo R ITEM   mark news items
licences POLICY [--target T]     package.license/00-porthole-licences
global-use JSON [--target T]     package.use/00-porthole-global
profile NAME [--target T]        switch make.profile (only a listed profile)
repo add|remove|sync NAME ...    repos.conf/<name>.conf
binhost add|remove NAME ...      binrepos.conf/<name>.conf
binpkgs clean|index|upload       jobs on the package directory
session [--minutes N]            the Unlock helper
```

## Files and state

| Path | What |
|---|---|
| `/etc/porthole/updater.conf` | schedule, automation level, build window, binary packages (root's; documented in `data/updater.conf`) |
| `/var/lib/porthole/plan.json`, `state.json`, `choices.json` | latest scheduled plan, check/sync times, standing choices |
| `/var/lib/porthole/jobs.json`, `/var/log/porthole/jobs/` | the job queue and job logs |
| `/var/lib/porthole/installs/`, `news/` | root-made install plans; Porthole's own news items |
| `/etc/portage/package.*/zz-porthole` | approved changes for single packages, commented |
| `/etc/portage/package.use/00-porthole-global`, `package.license/00-porthole-licences` | settings made in Settings > System |
| `~/.cache/porthole/`, `~/.local/state/porthole/` | the user's plans, install queue, tray state |
| `/run/user/UID/porthole-activity` | idle time written by the tray, read by the runner for the build window |

`make.conf` is never written.

## Systems ("targets")

`porthole/updater/targets.py` names the systems Porthole manages: this
computer (`local`, config root and root `/`) plus any in
`/etc/porthole/targets.conf` (crossdev roots, chroots). Plans record their
target; the runner writes fixes into that target's `etc/portage`, resolving
the target by name at run time. Only `local` is checked on a schedule so far;
this is the groundwork for the crossdev and remote-machine plugins.

## Tests

All run as a normal user; none change the system.

```
python3 tests/updater/scenarios.py            # 21 fake systems through the resolver
python3 tests/updater/autopilot_scenarios.py  # fully automatic decisions on them
python3 tests/updater/service_args.py         # every service command line parses
python3 tests/updater/system_test.py          # make.conf reading, findings, global USE
python3 tests/updater/targets_test.py         # targets.conf and per-target writes
python3 tests/updater/review_ui.py SRC OUT SCENARIO [LABEL...]   # review window, needs a display
```

`scenarios.py` builds a repository, a config root and an installed root in a
temporary directory and runs the analyzer against them with `--config-root`
and `--root`, so cycles, blockers, keyword/USE/licence/mask fixes, orphans
and held-back packages can be tested without touching the real system.

## Packaging

`setup.py` installs the package, the five scripts, glade files, help and
data. The ebuild installs `/etc/porthole/updater.conf`, the hourly cron job
and the tray's autostart entry (USE=notifier). Runtime needs: pygobject,
GTK 3, libnotify, libayatana-appindicator (or an XEmbed tray), libXss (idle
time), sudo or polkit; optional: smart-live-rebuild (live packages),
gentoolkit (`eclean-pkg`), rsync (uploading binary packages).
