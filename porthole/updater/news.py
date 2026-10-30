"""
    Porthole: Gentoo news

    Lists the news items portage considers relevant (the ones eselect news
    shows), lets the user read them and mark them read or unread. Reading
    needs no root; the read state lives in /var/lib/gentoo/news, which
    only root (and the portage group) may change, so marking goes through
    `porthole-update-service news` as root.

    Copyright (C) 2026 Porthole developers

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.
"""

import locale
import os
import re

NEWS_STATE = "/var/lib/gentoo/news"
# Porthole's own items (what an automatic update decided), in the same
# format, with their own unread list; shown as repository "porthole"
PORTHOLE = "porthole"
PORTHOLE_NEWS = "/var/lib/porthole/news"
PORTHOLE_UNREAD = os.path.join(PORTHOLE_NEWS, "unread")
KEEP_ITEMS = 50
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _lines(path):
    try:
        with open(path) as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def repositories():
    """{repo name: news directory} for repositories with news"""
    found = {}
    try:
        import portage
        for repo in portage.db[portage.root]["vartree"].settings.repositories:
            path = os.path.join(repo.location, "metadata", "news")
            if os.path.isdir(path):
                found[repo.name] = path
    except Exception:
        pass
    return found


def _item_file(directory, name):
    """The item's text in the user's language, else English, else any"""
    folder = os.path.join(directory, name)
    lang = (locale.getlocale()[0] or "en").split("_")[0]
    for candidate in (lang, "en"):
        path = os.path.join(folder, "%s.%s.txt" % (name, candidate))
        if os.path.exists(path):
            return path
    try:
        return next(os.path.join(folder, f) for f in sorted(os.listdir(folder))
                    if f.endswith(".txt"))
    except (OSError, StopIteration):
        return None


def parse(path):
    """(headers, body) of a news item file"""
    headers, body = {}, []
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return headers, ""
    head, _sep, body = text.partition("\n\n")
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            headers.setdefault(key.strip(), value.strip())
    return headers, body.strip()


def _sources():
    """(repo, news directory, unread names, relevant names) for each source"""
    for repo, directory in repositories().items():
        unread = _lines(os.path.join(NEWS_STATE, "news-%s.unread" % repo))
        # .skip lists every relevant item portage has seen; .unread those not read
        relevant = _lines(os.path.join(NEWS_STATE, "news-%s.skip" % repo))
        yield repo, directory, unread, relevant + [n for n in unread if n not in relevant]
    yield PORTHOLE, PORTHOLE_NEWS, _lines(PORTHOLE_UNREAD), _porthole_items()


def _porthole_items():
    return sorted(n for n in _listdir(PORTHOLE_NEWS)
                  if _NAME.match(n) and os.path.isdir(os.path.join(PORTHOLE_NEWS, n)))


def items():
    """Relevant news items, newest first: [{repo, name, title, posted,
    author, unread, path}]"""
    found = []
    for repo, directory, unread, relevant in _sources():
        unread = set(unread)
        for name in relevant:
            path = _item_file(directory, name)
            if not path:
                continue
            headers, _body = parse(path)
            found.append({"repo": repo, "name": name, "path": path,
                          "title": headers.get("Title", name),
                          "posted": headers.get("Posted", name[:10]),
                          "author": headers.get("Author", ""),
                          "unread": name in unread})
    return sorted(found, key=lambda i: (i["posted"], i["name"]), reverse=True)


def unread_count():
    return sum(len(_lines(path)) for path in
               [os.path.join(NEWS_STATE, f) for f in _listdir(NEWS_STATE)
                if f.endswith(".unread")] + [PORTHOLE_UNREAD])


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def set_read(repo, names, read=True):
    """Mark items read (or unread again), as root: only items portage
    lists as relevant for that repository"""
    if not _NAME.match(repo or ""):
        raise ValueError("Not a repository name: %s" % repo)
    if repo == PORTHOLE:
        unread_file = PORTHOLE_UNREAD
        relevant = set(_porthole_items())
    else:
        unread_file = os.path.join(NEWS_STATE, "news-%s.unread" % repo)
        relevant = set(_lines(os.path.join(NEWS_STATE, "news-%s.skip" % repo)))
    unread = _lines(unread_file)
    relevant |= set(unread)
    for name in names:
        if not _NAME.match(name) or name not in relevant:
            raise ValueError("Not a news item of %s: %s" % (repo, name))
    if read:
        unread = [n for n in unread if n not in names]
    else:
        unread += [n for n in names if n not in unread]
    stat = os.stat(unread_file) if os.path.exists(unread_file) else None
    tmp = unread_file + ".porthole"
    with open(tmp, "w") as f:
        f.write("".join(n + "\n" for n in unread))
    if stat:
        os.chown(tmp, stat.st_uid, stat.st_gid)
        os.chmod(tmp, stat.st_mode & 0o7777)
    os.replace(tmp, unread_file)
    return len(unread)


def write_porthole_item(title, body, name=None):
    """Add (or with name, replace) one of Porthole's own news items, as
    root, unread. Returns its name."""
    import time
    if not name:
        base = name = time.strftime("%Y-%m-%d-update-%H%M%S")
        n = 1
        while os.path.exists(os.path.join(PORTHOLE_NEWS, name)):
            n += 1
            name = "%s-%d" % (base, n)
    folder = os.path.join(PORTHOLE_NEWS, name)
    os.makedirs(folder, mode=0o755, exist_ok=True)
    path = os.path.join(folder, "%s.en.txt" % name)
    text = ("Title: %s\nAuthor: Porthole\nContent-Type: text/plain\nPosted: %s\n"
            "Revision: 1\nNews-Item-Format: 2.0\n\n%s"
            % (title, name[:10], body))
    with open(path + ".new", "w") as f:
        f.write(text)
    os.chmod(path + ".new", 0o644)
    os.replace(path + ".new", path)
    unread = _lines(PORTHOLE_UNREAD)
    if name not in unread:
        with open(PORTHOLE_UNREAD + ".new", "w") as f:
            f.write("".join(n + "\n" for n in unread + [name]))
        os.chmod(PORTHOLE_UNREAD + ".new", 0o644)
        os.replace(PORTHOLE_UNREAD + ".new", PORTHOLE_UNREAD)
    import shutil
    for old in _porthole_items()[:-KEEP_ITEMS]:
        shutil.rmtree(os.path.join(PORTHOLE_NEWS, old), ignore_errors=True)
    return name


def append_porthole_item(name, text):
    """Add to one of Porthole's items (the result of its build), as root,
    and mark it unread again"""
    if not _NAME.match(name or ""):
        return
    path = os.path.join(PORTHOLE_NEWS, name, "%s.en.txt" % name)
    headers, body = parse(path)
    if not headers:
        return
    write_porthole_item(headers.get("Title", name), body + "\n" + text, name)


# -- the window ------------------------------------------------------------------

def window():
    """The news window (made on first use, then reused)"""
    global _window
    if _window is None:
        _window = NewsWindow()
    _window.refresh()
    _window.present()
    return _window


_window = None

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk, Pango
except (ImportError, ValueError):  # the service imports this module too
    Gtk = None

if Gtk is not None:
    class NewsWindow(Gtk.Window):
        def __init__(self):
            super().__init__(title="Porthole: News")
            from porthole.updater.commands import icon_path
            if icon_path():
                self.set_icon_from_file(icon_path())
            self.set_default_size(900, 560)
            self.connect("delete-event", lambda *a: self.hide() or True)
            self.listeners = []

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
            self.heading = Gtk.Label(xalign=0)
            box.pack_start(self.heading, False, False, 0)
            paned = Gtk.Paned(position=380)
            # repo, name, posted, title, unread, weight
            self.store = Gtk.ListStore(str, str, str, str, bool, int)
            self.view = Gtk.TreeView(model=self.store)
            self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
            for i, title in ((2, "Posted"), (3, "Title")):
                cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END if i == 3 else
                                            Pango.EllipsizeMode.NONE)
                column = Gtk.TreeViewColumn(title, cell, text=i, weight=5)
                column.set_resizable(True)
                column.set_expand(i == 3)
                self.view.append_column(column)
            self.view.get_selection().connect("changed", self.on_selected)
            scroller = Gtk.ScrolledWindow()
            scroller.add(self.view)
            paned.pack1(scroller, True, False)
            self.text = Gtk.TextView(editable=False, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD, left_margin=8, right_margin=8)
            buf = self.text.get_buffer()
            buf.create_tag("title", weight=Pango.Weight.BOLD, scale=1.2)
            buf.create_tag("meta", style=Pango.Style.ITALIC)
            text_scroller = Gtk.ScrolledWindow()
            text_scroller.add(self.text)
            paned.pack2(text_scroller, True, False)
            box.pack_start(paned, True, True, 0)

            buttons = Gtk.Box(spacing=6)
            close = Gtk.Button(label="Close")
            close.connect("clicked", lambda b: self.hide())
            self.read_button = Gtk.Button(label="Mark read")
            self.read_button.connect("clicked", lambda b: self.mark(self.selected(), True))
            self.unread_button = Gtk.Button(label="Mark unread")
            self.unread_button.connect("clicked", lambda b: self.mark(self.selected(), False))
            self.all_button = Gtk.Button(label="Mark all read")
            self.all_button.connect("clicked", lambda b: self.mark(
                [(r[0], r[1]) for r in self.store if r[4]], True))
            buttons.pack_start(close, False, False, 0)
            buttons.pack_end(self.all_button, False, False, 0)
            buttons.pack_end(self.unread_button, False, False, 0)
            buttons.pack_end(self.read_button, False, False, 0)
            box.pack_start(buttons, False, False, 0)
            self.add(box)
            box.show_all()
            self.paths = {}

        def refresh(self):
            selected = set(self.selected())
            self.store.clear()
            self.paths = {}
            all_items = items()
            for item in all_items:
                self.paths[(item["repo"], item["name"])] = (item["path"], item)
                it = self.store.append([item["repo"], item["name"], item["posted"], item["title"],
                                        item["unread"], 700 if item["unread"] else 400])
                if (item["repo"], item["name"]) in selected:
                    self.view.get_selection().select_iter(it)
            unread = sum(1 for i in all_items if i["unread"])
            self.heading.set_markup("<b>%d news item%s, %d unread.</b> Marking items read "
                                    "or unread asks for administrator rights."
                                    % (len(all_items), "s" if len(all_items) != 1 else "", unread))
            self.all_button.set_sensitive(unread > 0)
            self.on_selected(self.view.get_selection())

        def selected(self):
            model, paths = self.view.get_selection().get_selected_rows()
            return [(model[p][0], model[p][1]) for p in paths]

        def on_selected(self, selection):
            chosen = self.selected()
            rows = {(r[0], r[1]): r[4] for r in self.store}
            self.read_button.set_sensitive(any(rows.get(c) for c in chosen))
            self.unread_button.set_sensitive(any(not rows.get(c, True) for c in chosen))
            buf = self.text.get_buffer()
            buf.set_text("")
            if len(chosen) != 1 or chosen[0] not in self.paths:
                return
            path, item = self.paths[chosen[0]]
            _headers, body = parse(path)
            buf.insert_with_tags_by_name(buf.get_end_iter(), item["title"] + "\n", "title")
            buf.insert_with_tags_by_name(buf.get_end_iter(), "%s   %s\n\n" % (
                item["posted"], item["author"]), "meta")
            buf.insert(buf.get_end_iter(), body + "\n")

        def mark(self, chosen, read):
            from porthole.updater.elevate import run_as_root, service
            by_repo = {}
            for repo, name in chosen:
                by_repo.setdefault(repo, []).append(name)
            for repo, names in by_repo.items():
                run_as_root(service("news", "read" if read else "unread", "--repo", repo, *names),
                            self.marked)

        def marked(self, ok, output):
            if not ok:
                dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                           message_type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.CLOSE,
                                           text="The news wasn't marked")
                dialog.format_secondary_text(output or "Cancelled.")
                dialog.run()
                dialog.destroy()
            self.refresh()
            for listener in self.listeners:
                GLib.idle_add(listener)
