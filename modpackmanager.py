#!/usr/bin/env python3
"""
modpack_manager.py - TUI manager for a Modrinth modpack source tree (.mrpack).

Run it from the repo root (it expects src/modrinth.index.json and
src/overrides/mods, same as get-hashes.py):

    python modpack_manager.py                 # interactive TUI
    python modpack_manager.py check           # report problems, exit 1 if any
    python modpack_manager.py fix             # remove duplicate entries and save
    python modpack_manager.py fix --newest    # ...and keep only the newest version
                                              #    of mods listed with 2+ versions
    python modpack_manager.py convert         # jars in overrides/mods -> index entries
    python modpack_manager.py --index PATH --mods PATH   # custom locations

Needs: Python 3.8+, `requests`. The TUI uses the stdlib `curses` module.
Only the free public Modrinth API is used (no account or token needed).
"""

from __future__ import annotations

import argparse
import curses
import hashlib
import json
import locale
import os
import re
import sys
import textwrap
import time
from collections import defaultdict, namedtuple
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

API = os.environ.get("MODRINTH_API", "https://api.modrinth.com/v2")
USER_AGENT = "Pavle012/modpack-manager (personal modpack tool)"
CACHE_DIR = Path(
    os.environ.get("MODPACK_MANAGER_CACHE")
    or (Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "modpack-manager")
)
CACHE_FILE = CACHE_DIR / "cache.json"
BACKUP_DIR = CACHE_DIR / "backups"

# Hosts the .mrpack format allows in "downloads"
ALLOWED_HOSTS = ("cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com")
# index.json dependency key -> Modrinth loader name
LOADERS = {"neoforge": "neoforge", "forge": "forge", "fabric-loader": "fabric", "quilt-loader": "quilt"}

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
CDN_RE = re.compile(r"/data/([A-Za-z0-9]+)/versions/([A-Za-z0-9]+)/")
ENTRY_KEY_ORDER = ("path", "hashes", "env", "downloads", "fileSize")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fit(text, width):
    text = str(text)
    if len(text) > width:
        return text[: max(0, width - 1)] + "…"
    return text.ljust(width)


def fmt_int(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def entry_sha1(entry):
    s = str((entry.get("hashes") or {}).get("sha1", "")).lower()
    return s if SHA1_RE.match(s) else None


def url_ids(entry):
    """(project_id, version_id) parsed from a cdn.modrinth.com download URL."""
    for u in entry.get("downloads") or []:
        m = CDN_RE.search(u)
        if m:
            return m.group(1), m.group(2)
    return None, None


def url_filename(entry):
    u = (entry.get("downloads") or [""])[0]
    return unquote(u.rsplit("/", 1)[-1].split("?")[0])


def canonical(entry):
    """Entry with keys in the order Modrinth tools write them."""
    out = {k: entry[k] for k in ENTRY_KEY_ORDER if k in entry}
    out.update({k: v for k, v in entry.items() if k not in out})
    return out


def best_version(versions):
    """Newest release if there is one, otherwise the newest of anything."""
    for v in versions:
        if v.get("version_type") == "release":
            return v
    return versions[0] if versions else None


def primary_file(version):
    files = version.get("files") or []
    return next((f for f in files if f.get("primary")), files[0] if files else None)


# --------------------------------------------------------------------------- #
# Modrinth API (+ small on-disk cache so names don't need re-fetching)
# --------------------------------------------------------------------------- #

class ApiError(Exception):
    pass


def slim_version(v):
    return {k: v.get(k) for k in ("id", "project_id", "version_number", "version_type", "date_published", "name")}


def slim_project(p):
    return {k: p.get(k) for k in ("title", "slug", "client_side", "server_side")}


class Modrinth:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.vcache = {}      # sha1 -> slim version
        self.pcache = {}      # project id -> slim project
        self.missing = set()  # sha1s Modrinth doesn't know (this run only)
        try:
            d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            self.vcache = d.get("versions", {})
            self.pcache = d.get("projects", {})
        except (OSError, ValueError):
            pass

    def flush(self):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({"versions": self.vcache, "projects": self.pcache}), encoding="utf-8")
        except OSError:
            pass

    def _request(self, method, path, **kw):
        last = None
        for attempt in range(3):
            try:
                r = self.s.request(method, API + path, timeout=30, **kw)
            except requests.RequestException as e:
                last = f"network error: {type(e).__name__}"
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                break  # offline: don't keep the user waiting
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                try:
                    wait = float(r.headers.get("Retry-After", 2))
                except ValueError:
                    wait = 2
                time.sleep(min(wait, 10))
                continue
            if r.status_code >= 400:
                raise ApiError(f"{method} {path} -> HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError:
                raise ApiError(f"{method} {path}: invalid JSON in response")
        raise ApiError(f"{method} {path}: {last}")

    # ---- lookups -------------------------------------------------------- #

    def search(self, query, mc, loader, limit=30):
        facets = [["project_type:mod"]]
        if mc:
            facets.append([f"versions:{mc}"])
        if loader:
            facets.append([f"categories:{loader}"])
        params = {
            "query": query,
            "facets": json.dumps(facets),
            "limit": limit,
            "index": "relevance" if query else "downloads",
        }
        hits = self._request("GET", "/search", params=params).get("hits", [])
        for h in hits:
            self.pcache[h["project_id"]] = slim_project(h)
        self.flush()
        return hits

    def project(self, ref):
        p = self._request("GET", f"/project/{ref}")
        self.pcache[p["id"]] = slim_project(p)
        self.flush()
        return {"project_id": p["id"], "title": p.get("title"), "description": p.get("description", ""),
                "downloads": p.get("downloads", 0)}

    def project_versions(self, project_id, loader, mc):
        params = {}
        if loader:
            params["loaders"] = json.dumps([loader])
        if mc:
            params["game_versions"] = json.dumps([mc])
        return self._request("GET", f"/project/{project_id}/version", params=params)

    def version(self, version_id):
        return self._request("GET", f"/version/{version_id}")

    def versions_by_hash(self, hashes):
        hashes = list(dict.fromkeys(hashes))
        out = {}
        for part in chunks(hashes, 200):
            res = self._request("POST", "/version_files", json={"hashes": part, "algorithm": "sha1"})
            for h, v in res.items():
                out[h] = v
                self.vcache[h] = slim_version(v)
        for h in hashes:
            if h not in out:
                self.missing.add(h)
        self.flush()
        return out

    def latest_by_hash(self, hashes, loader, mc):
        body = {"hashes": list(dict.fromkeys(hashes)), "algorithm": "sha1"}
        if loader:
            body["loaders"] = [loader]
        if mc:
            body["game_versions"] = [mc]
        out = {}
        for part in chunks(body["hashes"], 200):
            out.update(self._request("POST", "/version_files/update", json={**body, "hashes": part}))
        return out

    def ensure_versions(self, hashes):
        need = [h for h in dict.fromkeys(hashes) if h not in self.vcache and h not in self.missing]
        if need:
            self.versions_by_hash(need)

    def ensure_projects(self, ids):
        need = [p for p in dict.fromkeys(ids) if p and p not in self.pcache]
        for part in chunks(need, 50):
            for p in self._request("GET", "/projects", params={"ids": json.dumps(part)}):
                self.pcache[p["id"]] = slim_project(p)
        if need:
            self.flush()

    def remember(self, version, file):
        h = (file.get("hashes") or {}).get("sha1")
        if h:
            self.vcache[h] = slim_version(version)

    def vmeta(self, sha1):
        return self.vcache.get(sha1) if sha1 else None

    def pmeta(self, pid):
        return self.pcache.get(pid) if pid else None


# --------------------------------------------------------------------------- #
# The pack (modrinth.index.json + overrides/mods)
# --------------------------------------------------------------------------- #

def make_entry(version, file, project):
    entry = {
        "path": "mods/" + file["filename"],
        "hashes": {"sha1": file["hashes"]["sha1"], "sha512": file["hashes"]["sha512"]},
    }
    if project:
        def norm(v):
            return v if v in ("required", "optional", "unsupported") else "optional"
        entry["env"] = {"client": norm(project.get("client_side")), "server": norm(project.get("server_side"))}
    entry["downloads"] = [file["url"]]
    entry["fileSize"] = file["size"]
    return entry


class Pack:
    def __init__(self, index_path: Path, mods_dir: Path):
        self.index_path = index_path
        self.mods_dir = mods_dir
        self.data = json.loads(index_path.read_text(encoding="utf-8"))
        self.data.setdefault("files", [])
        self.dirty = False
        self.pending_delete = []  # local jars to delete from overrides on save

    @property
    def files(self):
        return self.data["files"]

    @property
    def changed(self):
        return self.dirty or bool(self.pending_delete)

    @property
    def mc(self):
        return (self.data.get("dependencies") or {}).get("minecraft")

    @property
    def loader_key(self):
        deps = self.data.get("dependencies") or {}
        return next((k for k in LOADERS if k in deps), None)

    @property
    def loader(self):
        k = self.loader_key
        return LOADERS[k] if k else None

    def local_jars(self):
        if not self.mods_dir.is_dir():
            return []
        return sorted((p for p in self.mods_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jar"),
                      key=lambda p: p.name.lower())

    def project_ids(self):
        return {pid for pid, _ in (url_ids(e) for e in self.files) if pid}

    def remove_entries(self, entries):
        ids = {id(e) for e in entries}
        self.data["files"] = [e for e in self.files if id(e) not in ids]
        self.dirty = True

    def apply_version(self, version, project, file=None):
        """Add `version` (replacing any entry of the same project). Returns replaced entries."""
        file = file or primary_file(version)
        if not file:
            raise ApiError(f"version {version.get('version_number')} has no files")
        new = make_entry(version, file, project)
        new_sha = new["hashes"]["sha1"]
        pid = version["project_id"]
        old = [e for e in self.files
               if url_ids(e)[0] == pid or entry_sha1(e) == new_sha or e.get("path") == new["path"]]
        if old:
            pos = self.files.index(old[0])
            self.data["files"] = [e for e in self.files if all(e is not o for o in old)]
            self.files.insert(pos, new)
        else:
            self.files.append(new)
        self.dirty = True
        return old

    def save(self):
        """Back up, write the index (tabs + trailing newline, like your current file), delete converted jars."""
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (BACKUP_DIR / f"{stamp}-{self.index_path.name}").write_text(
                self.index_path.read_text(encoding="utf-8"), encoding="utf-8")
            for old in sorted(BACKUP_DIR.iterdir())[:-30]:
                old.unlink()
        except OSError:
            pass
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent="\t") + "\n", encoding="utf-8")
        tmp.replace(self.index_path)
        for p in self.pending_delete:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self.pending_delete = []
        self.dirty = False


# --------------------------------------------------------------------------- #
# Analysis / fixing (the part that repairs a broken index)
# --------------------------------------------------------------------------- #

Analysis = namedtuple("Analysis", "dup conflict multi bad host lines problems")


def multi_groups(files):
    """project id -> indices, for projects present with 2+ different versions."""
    g = defaultdict(list)
    for i, e in enumerate(files):
        pid, vid = url_ids(e)
        if pid:
            g[pid].append((i, vid))
    return {pid: [i for i, _ in lst] for pid, lst in g.items() if len({v for _, v in lst}) > 1}


def analyze(files):
    dup, conflict, bad, host = set(), set(), set(), set()
    by_path, by_sha = defaultdict(list), defaultdict(list)
    for i, e in enumerate(files):
        hashes = e.get("hashes") or {}
        sha = str(hashes.get("sha1", "")).lower()
        if (not SHA1_RE.match(sha) or not hashes.get("sha512") or not e.get("path")
                or not e.get("downloads") or not isinstance(e.get("fileSize"), int)):
            bad.add(i)
        by_path[e.get("path")].append((i, sha))
        if SHA1_RE.match(sha):
            by_sha[sha].append(i)
        for u in e.get("downloads") or []:
            if urlparse(u).hostname not in ALLOWED_HOSTS:
                host.add(i)
    for lst in by_path.values():
        if len(lst) > 1:
            (dup if len({s for _, s in lst}) == 1 else conflict).update(i for i, _ in lst)
    for idxs in by_sha.values():
        if len(idxs) > 1:
            dup.update(idxs)
    groups = multi_groups(files)
    multi = {i for idxs in groups.values() for i in idxs}

    lines = []
    dup_files = len({entry_sha1(files[i]) for i in dup})
    if dup:
        lines.append(f"{dup_files} file(s) listed more than once in the index")
    if conflict:
        lines.append(f"{len(conflict)} entries share a path but are different files (fix by hand)")
    if multi:
        lines.append(f"{len(groups)} mod(s) are in the pack with 2+ different versions")
    if bad:
        lines.append(f"{len(bad)} entries have missing/invalid hashes, URL or size")
    if host:
        lines.append(f"{len(host)} entries download from a host Modrinth does not allow")
    return Analysis(dup, conflict, multi, bad, host, lines, bool(lines))


def fix_index(pack):
    """Offline repair: drop duplicate entries (same sha1), normalise hash case."""
    msgs = []
    files = pack.files
    groups = defaultdict(list)
    for i, e in enumerate(files):
        sha = entry_sha1(e)
        if sha:
            groups[sha].append(i)
    drop = set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue

        def score(i):
            e = files[i]
            return (Path(e.get("path", "")).name == url_filename(e), bool(e.get("env")),
                    bool((e.get("hashes") or {}).get("sha512")))

        keep = max(idxs, key=lambda i: (score(i), -i))
        keeper = files[keep]
        for i in idxs:
            if i == keep:
                continue
            if not keeper.get("env") and files[i].get("env"):
                keeper["env"] = files[i]["env"]
                files[keep] = canonical(keeper)
            drop.add(i)
            same = files[i].get("path") == keeper.get("path")
            msgs.append(f"removed duplicate entry: {files[i].get('path')}"
                        + ("" if same else f"  (kept {keeper.get('path')})"))
    if drop:
        pack.data["files"] = [e for i, e in enumerate(files) if i not in drop]
        pack.dirty = True
    for e in pack.files:
        hs = e.get("hashes") or {}
        for k, v in list(hs.items()):
            if isinstance(v, str) and v != v.lower():
                hs[k] = v.lower()
                pack.dirty = True
                msgs.append(f"lower-cased {k} of {e.get('path')}")
    return msgs


def plan_keep_newest(pack, api):
    """Needs network. Returns (plan, skipped): plan = [(keep_entry, [drop_entries])]."""
    files = pack.files
    groups = multi_groups(files)
    hashes = [entry_sha1(files[i]) for idxs in groups.values() for i in idxs if entry_sha1(files[i])]
    api.ensure_versions(hashes)
    api.ensure_projects(groups.keys())
    plan, skipped = [], []
    for pid, idxs in groups.items():
        metas = [(api.vmeta(entry_sha1(files[i])), files[i]) for i in idxs]
        if any(m is None for m, _ in metas):
            skipped.append(pid)
            continue
        keep = max(metas, key=lambda me: me[0]["date_published"])[1]
        drops = [e for _, e in metas if url_ids(e)[1] != url_ids(keep)[1]]
        # entries that are exact repeats of the kept version are handled by fix_index
        plan.append((keep, drops))
    return plan, skipped


def describe_keep_newest(api, plan, skipped):
    lines = []
    for keep, drops in plan:
        pid = url_ids(keep)[0]
        name = (api.pmeta(pid) or {}).get("title") or pid

        def ver(e):
            return (api.vmeta(entry_sha1(e)) or {}).get("version_number") or Path(e["path"]).name

        lines.append(f"{name}: keep {ver(keep)}, remove {', '.join(ver(d) for d in drops)}")
    for pid in skipped:
        lines.append((f"skipped {pid}: Modrinth doesn't know one of its files", "warn"))
    return lines


def apply_keep_newest(pack, plan):
    drops = [d for _, ds in plan for d in ds]
    if drops:
        pack.remove_entries(drops)
    return len(drops)


def convert_local(pack, api):
    """Needs network. jars in overrides/mods -> index entries (jars are deleted on save)."""
    jars = pack.local_jars()
    if not jars:
        return ["no jars in overrides/mods"]
    by_hash = defaultdict(list)
    for p in jars:
        by_hash[sha1_file(p)].append(p)
    found = api.versions_by_hash(list(by_hash))
    api.ensure_projects({v["project_id"] for v in found.values()})
    msgs = []
    for h, paths in by_hash.items():
        v = found.get(h)
        f = next((f for f in (v or {}).get("files", []) if (f.get("hashes") or {}).get("sha1") == h), None)
        if not v or not f:
            msgs.append(f"kept in overrides (not on Modrinth): {paths[0].name}")
            continue
        old = pack.apply_version(v, api.pmeta(v["project_id"]), file=f)
        api.remember(v, f)
        same = [e for e in old if entry_sha1(e) == h]
        if old and len(same) == len(old):
            msgs.append(f"already in index, local copy removed: {paths[0].name}")
        else:
            extra = f"  (replaced {len(old)} older entr{'y' if len(old) == 1 else 'ies'})" if old else ""
            msgs.append(f"converted: {paths[0].name} -> {v['version_number']}{extra}")
        pack.pending_delete.extend(paths)
    api.flush()
    return msgs


# --------------------------------------------------------------------------- #
# curses helpers
# --------------------------------------------------------------------------- #

STYLE = {"": 0, "ok": 0, "warn": 0, "bad": 0, "head": 0, "dim": 0}
Result = namedtuple("Result", "key index selected flt")


def init_colors():
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        STYLE.update(ok=curses.color_pair(1), warn=curses.color_pair(2), bad=curses.color_pair(3),
                     head=curses.color_pair(4) | curses.A_BOLD, dim=curses.A_DIM)
    except curses.error:
        STYLE.update(head=curses.A_BOLD, bad=curses.A_BOLD, dim=curses.A_DIM)


def put(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w - 1:
        return
    try:
        scr.addnstr(y, x, text, w - x - 1, attr)
    except curses.error:
        pass


def busy(scr, text):
    h, w = scr.getmaxyx()
    put(scr, h - 2, 0, (" " + text).ljust(w - 1), STYLE["warn"] | curses.A_BOLD)
    scr.refresh()


def prompt(scr, label, default=""):
    """One-line editor on the bottom row. Returns the text, or None on Esc."""
    buf, pos = list(default), len(default)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            h, w = scr.getmaxyx()
            head = f" {label}: "
            avail = max(1, w - len(head) - 2)
            start = max(0, pos - avail + 1)
            put(scr, h - 1, 0, " " * (w - 1))
            put(scr, h - 1, 0, head, STYLE["head"])
            put(scr, h - 1, len(head), "".join(buf)[start:start + avail])
            try:
                scr.move(h - 1, min(w - 2, len(head) + pos - start))
            except curses.error:
                pass
            scr.refresh()
            ch = scr.get_wch()
            if ch == curses.KEY_RESIZE:
                continue
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return "".join(buf)
            if ch == "\x1b":
                return None
            if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if pos:
                    del buf[pos - 1]
                    pos -= 1
            elif ch == curses.KEY_DC:
                if pos < len(buf):
                    del buf[pos]
            elif ch == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif ch == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif ch in (curses.KEY_HOME, "\x01"):
                pos = 0
            elif ch in (curses.KEY_END, "\x05"):
                pos = len(buf)
            elif ch == "\x15":
                buf, pos = [], 0
            elif isinstance(ch, str) and ch.isprintable():
                buf.insert(pos, ch)
                pos += 1
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def ask(scr, text, choices="yn", default=None):
    label = " %s [%s] " % (text, "/".join(c.upper() if c == default else c for c in choices))
    while True:
        h, w = scr.getmaxyx()
        put(scr, h - 2, 0, label.ljust(w - 1), STYLE["warn"] | curses.A_BOLD | curses.A_REVERSE)
        scr.refresh()
        ch = scr.getch()
        if ch == curses.KEY_RESIZE:
            continue
        if ch in (10, 13, curses.KEY_ENTER):
            if default:
                return default
            continue
        if ch == 27:
            return None
        if 0 <= ch < 256 and chr(ch).lower() in choices:
            return chr(ch).lower()


def confirm(scr, text, default=False):
    return ask(scr, text, "yn", "y" if default else "n") == "y"


def show_text(scr, title, lines, confirm_mode=False):
    """Scrollable pager. With confirm_mode returns True (y) / False (n, Esc)."""
    lines = [(l, "") if isinstance(l, str) else tuple(l) for l in lines]
    top = 0
    while True:
        h, w = scr.getmaxyx()
        wrapped = []
        for text, style in lines:
            parts = textwrap.wrap(text, max(10, w - 4), subsequent_indent="    ") or [""]
            wrapped.extend((p, style) for p in parts)
        view = max(1, h - 3)
        top = max(0, min(top, max(0, len(wrapped) - view)))
        scr.erase()
        put(scr, 0, 0, (" " + title).ljust(w - 1), STYLE["head"])
        for n in range(view):
            if top + n >= len(wrapped):
                break
            text, style = wrapped[top + n]
            put(scr, 1 + n, 1, text, STYLE[style])
        more = f"  ({top + 1}-{min(len(wrapped), top + view)}/{len(wrapped)})" if len(wrapped) > view else ""
        help_line = "y = yes   n / Esc = no" if confirm_mode else "Up/Down scroll   Enter / Esc / q close"
        put(scr, h - 1, 0, (" " + help_line + more).ljust(w - 1), STYLE["dim"])
        scr.refresh()
        ch = scr.getch()
        c = chr(ch).lower() if 0 <= ch < 256 else ""
        if ch == curses.KEY_RESIZE:
            continue
        if confirm_mode:
            if c == "y":
                return True
            if c == "n" or ch == 27 or c == "q":
                return False
        elif ch in (10, 13, 27, curses.KEY_ENTER) or c == "q":
            return None
        if ch == curses.KEY_UP or c == "k":
            top -= 1
        elif ch == curses.KEY_DOWN or c == "j":
            top += 1
        elif ch == curses.KEY_PPAGE:
            top -= view
        elif ch == curses.KEY_NPAGE or c == " ":
            top += view
        elif ch == curses.KEY_HOME:
            top = 0
        elif ch == curses.KEY_END:
            top = len(wrapped)


def pick(scr, title, rows, *, multi=False, hotkeys=(), selected=(), cursor=0, header=(),
         help_line="", status="", flt=""):
    """Scrollable list. rows: str or (str, style). Returns Result(key, index, selected, flt)
    where key is 'enter', 'esc' or one of `hotkeys`; index is the row under the cursor."""
    rows = [(r, "") if isinstance(r, str) else tuple(r) for r in rows]
    sel = set(selected)
    hotkeys = set(hotkeys)
    top, pos, first = 0, 0, True
    while True:
        low = flt.lower()
        vis = [i for i, (t, _) in enumerate(rows) if low in t.lower()]
        if first:
            pos = vis.index(cursor) if cursor in vis else 0
            first = False
        pos = max(0, min(pos, len(vis) - 1)) if vis else 0
        h, w = scr.getmaxyx()
        scr.erase()
        put(scr, 0, 0, (" " + title).ljust(w - 1), STYLE["head"])
        y = 1
        for line in header:
            text, style = (line, "") if isinstance(line, str) else line
            put(scr, y, 1, text, STYLE[style])
            y += 1
        list_h = max(1, h - y - 2)
        if pos < top:
            top = pos
        if pos >= top + list_h:
            top = pos - list_h + 1
        for n in range(list_h):
            k = top + n
            if k >= len(vis):
                break
            i = vis[k]
            text, style = rows[i]
            prefix = ("[x] " if i in sel else "[ ] ") if multi else ""
            if k == pos:
                put(scr, y + n, 0, ("> " + prefix + text).ljust(w - 1), STYLE[style] | curses.A_REVERSE)
            else:
                put(scr, y + n, 0, "  " + prefix + text, STYLE[style])
        if not vis:
            put(scr, y, 2, "(nothing to show)", STYLE["dim"])
        info = f"{len(vis)}/{len(rows)}"
        if flt:
            info += f"  filter: {flt}"
        if multi:
            info += f"  selected: {len(sel)}"
        put(scr, h - 2, 0, " " + info, STYLE["dim"])
        if status:
            put(scr, h - 2, len(info) + 4, status, STYLE["warn"] | curses.A_BOLD)
        put(scr, h - 1, 0, (" " + help_line).ljust(w - 1), STYLE["dim"])
        scr.refresh()

        ch = scr.getch()
        if ch == curses.KEY_RESIZE:
            continue
        c = chr(ch) if 0 <= ch < 256 else ""
        cur_idx = vis[pos] if vis else None
        if c and c in hotkeys:
            return Result(c, cur_idx, sel, flt)
        if ch == curses.KEY_UP or c == "k":
            pos -= 1
        elif ch == curses.KEY_DOWN or c == "j":
            pos += 1
        elif ch == curses.KEY_PPAGE:
            pos -= list_h
        elif ch == curses.KEY_NPAGE:
            pos += list_h
        elif ch == curses.KEY_HOME or c == "g":
            pos = 0
        elif ch == curses.KEY_END or c == "G":
            pos = len(vis) - 1
        elif c == " " and multi and vis:
            sel.symmetric_difference_update({vis[pos]})
            pos += 1
        elif c == "*" and multi:
            if all(i in sel for i in vis):
                sel.difference_update(vis)
            else:
                sel.update(vis)
        elif c == "/":
            new = prompt(scr, "Filter (empty clears)", flt)
            if new is not None:
                flt, top, pos = new, 0, 0
        elif ch in (10, 13, curses.KEY_ENTER):
            return Result("enter", cur_idx, sel, flt)
        elif ch == 27 or c == "q":
            return Result("esc", cur_idx, sel, flt)


# --------------------------------------------------------------------------- #
# The TUI application
# --------------------------------------------------------------------------- #

Item = namedtuple("Item", "key kind entry path text style")

HELP = [
    "Keys in the mod list",
    "  Up/Down j/k  move          PgUp/PgDn  page          g / G  top / bottom",
    "  Space        select        *          select all    /      filter (type text, Enter)",
    "  Enter        details for the mod under the cursor",
    "",
    "  a  add mods from Modrinth (search by name, or paste a modrinth.com URL / slug)",
    "  d  remove selected mods (or the one under the cursor)",
    "  u  check for updates and apply the ones you pick",
    "  f  fix the index: removes duplicate entries, offers to keep only the newest",
    "     version of mods that are in the pack twice",
    "  c  convert jars in overrides/mods into index entries (like get-hashes.py)",
    "  p  pack info: edit version number / name / summary / MC + loader version",
    "  w  save    r  re-lookup names    q  quit",
    "",
    "Row flags:  DUP = listed twice   2VER = mod present with 2+ versions",
    "            BAD = invalid entry  LOCL = jar in overrides/mods, not in index",
    "",
    "Nothing is written to disk until you press w. A backup of the old index goes to",
    f"{BACKUP_DIR}.",
]


class App:
    def __init__(self, scr, pack, api):
        self.scr, self.pack, self.api = scr, pack, api
        self.sel, self.flt, self.status = set(), "", ""
        self.analysis = analyze(pack.files)

    # ---- lookups / display ------------------------------------------------ #

    def lookup(self):
        busy(self.scr, "Looking up mod names on Modrinth...")
        hashes = [h for h in (entry_sha1(e) for e in self.pack.files) if h]
        self.api.ensure_versions(hashes)
        pids = set(self.pack.project_ids())
        pids.update(m["project_id"] for m in (self.api.vmeta(h) for h in hashes) if m)
        self.api.ensure_projects(pids)

    def try_lookup(self):
        try:
            self.lookup()
        except ApiError as e:
            self.status = "offline: mod names unavailable (press r to retry)"

    def display(self, e):
        v = self.api.vmeta(entry_sha1(e))
        pid = (v or {}).get("project_id") or url_ids(e)[0]
        p = self.api.pmeta(pid)
        return (p or {}).get("title") or Path(e.get("path", "?")).stem, (v or {}).get("version_number") or ""

    def title_of(self, pid):
        return (self.api.pmeta(pid) or {}).get("title") or pid

    def build_items(self):
        a = self.analysis = analyze(self.pack.files)
        items = []
        for i, e in enumerate(self.pack.files):
            name, ver = self.display(e)
            flag, style = "", ""
            if i in a.bad or i in a.host or i in a.conflict:
                flag, style = "BAD", "bad"
            elif i in a.dup:
                flag, style = "DUP", "bad"
            elif i in a.multi:
                flag, style = "2VER", "warn"
            text = f"{flag:<4} {fit(name, 30)} {fit(ver, 16)} {Path(e.get('path', '?')).name}"
            items.append(Item(id(e), "index", e, None, text, style))
        for p in self.pack.local_jars():
            text = f"{'LOCL':<4} {fit(p.stem, 30)} {fit('(not in index)', 16)} {p.name}"
            items.append(Item(str(p), "local", None, p, text, "warn"))
        return items

    def header(self):
        p, a = self.pack, self.analysis
        loader = f"{p.loader_key} {p.data['dependencies'][p.loader_key]}" if p.loader_key else "no loader"
        unsaved = "   * UNSAVED CHANGES" if p.changed else ""
        lines = [(f"{p.data.get('name', '?')}  |  version {p.data.get('versionId', '?')}  |  "
                  f"MC {p.mc}  {loader}  |  {len(p.files)} in index, {len(p.local_jars())} local{unsaved}",
                  "warn" if p.changed else "")]
        if a.problems:
            lines += [("! " + l + "   (press f)", "bad") for l in a.lines[:4]]
        else:
            lines.append(("index looks OK", "ok"))
        return lines

    # ---- main loop ------------------------------------------------------- #

    def run(self):
        curses.curs_set(0)
        init_colors()
        self.try_lookup()
        cursor = 0
        while True:
            items = self.build_items()
            selected = {i for i, it in enumerate(items) if it.key in self.sel}
            res = pick(self.scr, "Modpack manager", [(it.text, it.style) for it in items], multi=True,
                       hotkeys="adufcpwrq?", selected=selected, cursor=cursor, header=self.header(),
                       help_line="a add  d remove  u update  f fix  c convert  p pack info  w save  "
                                 "/ filter  Space select  ? help  q quit",
                       status=self.status, flt=self.flt)
            self.sel = {items[i].key for i in res.selected if i < len(items)}
            self.flt = res.flt
            cursor = res.index or 0
            self.status = ""
            targets = [items[i] for i in sorted(res.selected) if i < len(items)]
            if not targets and res.index is not None and res.key == "d":
                targets = [items[res.index]]
            try:
                if res.key == "q":
                    if self.quit():
                        return
                elif res.key == "?":
                    show_text(self.scr, "Help", HELP)
                elif res.key == "a":
                    self.add_flow()
                elif res.key == "d":
                    self.remove_flow(targets)
                elif res.key == "u":
                    self.update_flow()
                elif res.key == "f":
                    self.fix_flow()
                elif res.key == "c":
                    self.convert_flow()
                elif res.key == "p":
                    self.pack_info()
                elif res.key == "w":
                    self.save()
                elif res.key == "r":
                    self.api.missing.clear()
                    self.lookup()
                    self.status = "names refreshed"
                elif res.key == "enter" and res.index is not None:
                    self.show_info(items[res.index])
            except ApiError as e:
                show_text(self.scr, "Modrinth API error", [str(e)])

    def quit(self):
        if not self.pack.changed:
            return True
        c = ask(self.scr, "Unsaved changes. Save before quitting?", "ynq", "y")
        if c == "y":
            self.save()
            return True
        return c == "n"

    def save(self):
        n_del = len(self.pack.pending_delete)
        self.pack.save()
        self.status = f"saved{f' (deleted {n_del} converted jar(s))' if n_del else ''}"

    # ---- details / remove ------------------------------------------------ #

    def show_info(self, it):
        if it.kind == "local":
            show_text(self.scr, it.path.name, [f"File:  {it.path}", f"Size:  {it.path.stat().st_size:,} bytes", "",
                                               "This jar is in overrides/mods and not in the index.",
                                               "Press c to look it up on Modrinth and convert it."])
            return
        e = it.entry
        name, ver = self.display(e)
        pid = url_ids(e)[0]
        env = e.get("env") or {}
        show_text(self.scr, name, [
            f"Name:     {name}", f"Version:  {ver or '(unknown)'}", f"Path:     {e.get('path')}",
            f"Project:  {pid or '(not a cdn.modrinth.com URL)'}" + (f"   https://modrinth.com/project/{pid}" if pid else ""),
            f"URL:      {(e.get('downloads') or ['-'])[0]}",
            f"sha1:     {(e.get('hashes') or {}).get('sha1')}", f"sha512:   {(e.get('hashes') or {}).get('sha512')}",
            f"Size:     {e.get('fileSize')}", f"Env:      client={env.get('client', '-')}  server={env.get('server', '-')}",
        ])

    def remove_flow(self, targets):
        if not targets:
            return
        lines = [(f"{'index' if t.kind == 'index' else 'LOCAL JAR (file will be deleted on save)'}:  "
                  f"{t.text[5:]}", "" if t.kind == "index" else "warn") for t in targets]
        if not show_text(self.scr, f"Remove {len(targets)} mod(s)?", lines, confirm_mode=True):
            return
        entries = [t.entry for t in targets if t.kind == "index"]
        if entries:
            self.pack.remove_entries(entries)
        for t in targets:
            if t.kind == "local" and t.path not in self.pack.pending_delete:
                self.pack.pending_delete.append(t.path)
        self.sel = set()
        self.status = f"removed {len(targets)} (press w to save)"

    # ---- add ---------------------------------------------------------------- #

    def add_flow(self):
        q = prompt(self.scr, "Search Modrinth (empty = most downloaded; or paste a modrinth.com URL)")
        if q is None:
            return
        q = q.strip()
        pack, api = self.pack, self.api
        m = re.search(r"modrinth\.com/(?:mod|project)/([^/?#\s]+)", q)
        busy(self.scr, "Searching...")
        if m or re.fullmatch(r"[A-Za-z0-9]{8}", q):
            try:
                hits = [api.project(m.group(1) if m else q)]
            except ApiError:
                hits = []
        else:
            hits = api.search(q, pack.mc, pack.loader)
        if not hits:
            show_text(self.scr, "Search", [f"No mods found for MC {pack.mc} / {pack.loader}."])
            return
        rows = [f"{fit(h['title'], 28)} {fmt_int(h.get('downloads')):>7} dl  {h.get('description', '')}" for h in hits]
        r = pick(self.scr, f"Results for '{q}'  (Enter = choose, Esc = back)", rows,
                 help_line="Enter choose   Esc back   / filter")
        if r.key != "enter" or r.index is None:
            return
        hit = hits[r.index]
        busy(self.scr, "Fetching versions...")
        versions = api.project_versions(hit["project_id"], pack.loader, pack.mc)
        if not versions:
            show_text(self.scr, hit["title"], [f"No version for MC {pack.mc} / {pack.loader}."])
            return
        style = {"release": "", "beta": "warn", "alpha": "bad"}
        rows = [(f"{fit(v['version_number'], 26)} {v['version_type']:<8} {v['date_published'][:10]}  {v['name']}",
                 style.get(v["version_type"], "")) for v in versions]
        first_release = next((i for i, v in enumerate(versions) if v["version_type"] == "release"), 0)
        r = pick(self.scr, f"{hit['title']}: choose a version  (Enter = add, Esc = back)", rows,
                 cursor=first_release, help_line="Enter add   Esc back")
        if r.key != "enter" or r.index is None:
            return
        self.install([versions[r.index]])

    def missing_deps(self, versions):
        """Required dependencies of `versions` that aren't in the pack yet."""
        pack, api = self.pack, self.api
        have = pack.project_ids() | {v["project_id"] for v in versions}
        found, warns, todo = [], [], list(versions)
        while todo and len(found) < 40:
            v = todo.pop()
            for dep in v.get("dependencies") or []:
                pid = dep.get("project_id")
                if dep.get("dependency_type") != "required" or not pid or pid in have:
                    continue
                have.add(pid)
                if dep.get("version_id"):
                    dv = api.version(dep["version_id"])
                else:
                    dv = best_version(api.project_versions(pid, pack.loader, pack.mc))
                if dv:
                    found.append(dv)
                    todo.append(dv)
                else:
                    warns.append(pid)
        return found, warns

    def install(self, versions, ask_plan=True):
        pack, api, scr = self.pack, self.api, self.scr
        busy(scr, "Checking required dependencies...")
        deps, warns = self.missing_deps(versions)
        api.ensure_projects([d["project_id"] for d in deps] + warns)
        extra = []
        if deps:
            rows = [f"{self.title_of(d['project_id'])}  {d['version_number']}" for d in deps]
            r = pick(scr, "Required dependencies missing from the pack", rows, multi=True,
                     selected=set(range(len(rows))),
                     help_line="Space toggle   Enter add the selected ones   Esc skip all")
            if r.key == "enter":
                extra = [deps[i] for i in sorted(r.selected)]
        todo = list(versions) + extra
        api.ensure_projects(v["project_id"] for v in todo)
        plan = []
        for v in todo:
            name = self.title_of(v["project_id"])
            old = [e for e in pack.files if url_ids(e)[0] == v["project_id"]]
            if any(url_ids(e)[1] == v["id"] for e in old):
                plan.append((f"= {name} {v['version_number']} is already in the pack", "dim"))
            elif old:
                oldv = ", ".join((api.vmeta(entry_sha1(e)) or {}).get("version_number") or Path(e["path"]).name
                                 for e in old)
                plan.append((f"~ {name}: {oldv} -> {v['version_number']}", "warn"))
            else:
                plan.append((f"+ {name} {v['version_number']}", "ok"))
        for pid in warns:
            plan.append((f"! required dependency {self.title_of(pid)} has no version for "
                         f"MC {pack.mc} / {pack.loader}; add it by hand", "bad"))
        if ask_plan and not show_text(scr, "Apply these changes?", plan, confirm_mode=True):
            return
        added = 0
        for v in todo:
            f = primary_file(v)
            if f is None or any(url_ids(e)[1] == v["id"] for e in pack.files):
                continue
            pack.apply_version(v, api.pmeta(v["project_id"]), file=f)
            api.remember(v, f)
            added += 1
        api.flush()
        self.status = f"{added} change(s) applied (press w to save)"

    # ---- update ------------------------------------------------------------- #

    def update_flow(self):
        pack, api, scr = self.pack, self.api, self.scr
        hashes = [h for h in (entry_sha1(e) for e in pack.files) if h]
        if not hashes:
            return
        pre = confirm(scr, "Include beta/alpha versions in updates?", False)
        busy(scr, "Checking Modrinth for newer versions...")
        api.ensure_versions(hashes)
        latest = api.latest_by_hash(hashes, pack.loader, pack.mc)
        cands = []
        for e in pack.files:
            h = entry_sha1(e)
            cur, new = api.vmeta(h), latest.get(h)
            if not cur or not new or new["id"] == cur["id"] or new["date_published"] <= cur["date_published"]:
                continue
            if new["version_type"] != "release" and not pre and cur["version_type"] == "release":
                busy(scr, f"Looking for a newer release of {self.title_of(new['project_id'])}...")
                rel = [v for v in api.project_versions(new["project_id"], pack.loader, pack.mc)
                       if v["version_type"] == "release" and v["date_published"] > cur["date_published"]]
                if not rel:
                    continue
                new = rel[0]
            cands.append((e, cur, new))
        if not cands:
            show_text(scr, "Updates", ["Everything is up to date."])
            return
        api.ensure_projects(n["project_id"] for _, _, n in cands)
        style = {"release": "", "beta": "warn", "alpha": "bad"}
        rows = [(f"{fit(self.title_of(n['project_id']), 28)} {fit(c['version_number'], 16)} -> "
                 f"{fit(n['version_number'], 16)} {n['version_type']}", style.get(n["version_type"], ""))
                for _, c, n in cands]
        r = pick(scr, f"{len(cands)} update(s) available", rows, multi=True, selected=set(range(len(rows))),
                 help_line="Space toggle   * all/none   Enter apply the selected   Esc cancel")
        if r.key != "enter" or not r.selected:
            return
        chosen, seen = [], set()
        for i in sorted(r.selected):
            n = cands[i][2]
            if n["id"] not in seen:
                seen.add(n["id"])
                chosen.append(n)
        self.install(chosen, ask_plan=False)

    # ---- fix / convert ------------------------------------------------------ #

    def fix_flow(self):
        scr, pack, api = self.scr, self.pack, self.api
        report = fix_index(pack)
        a = analyze(pack.files)
        if a.multi:
            n = len({url_ids(pack.files[i])[0] for i in a.multi})
            report.append((f"{n} mod(s) are in the pack with 2+ different versions (see 2VER rows).", "warn"))
        if a.conflict or a.bad or a.host:
            report.append(("Some entries need manual attention (rows marked BAD): "
                           + "; ".join(l for l in a.lines if "version" not in l and "listed" not in l), "bad"))
        if not report:
            report = [("Nothing to fix. The index looks fine.", "ok")]
        show_text(scr, "Fix results", report)
        if a.multi and confirm(scr, "Keep only the newest version of each 2VER mod? (needs network)", False):
            busy(scr, "Comparing release dates on Modrinth...")
            plan, skipped = plan_keep_newest(pack, api)
            lines = describe_keep_newest(api, plan, skipped)
            if plan and show_text(scr, f"Remove old versions from {len(plan)} mod(s)?", lines, confirm_mode=True):
                self.status = f"removed {apply_keep_newest(pack, plan)} old entries (press w to save)"
            elif not plan:
                show_text(scr, "Nothing to do", lines or ["No removable entries found."])
        self.sel = set()

    def convert_flow(self):
        busy(self.scr, "Hashing local jars and asking Modrinth...")
        msgs = convert_local(self.pack, self.api)
        msgs.append("")
        msgs.append(("Converted jars are deleted from overrides/mods when you save (w).", "dim"))
        msgs.extend(fix_index(self.pack))
        show_text(self.scr, "Convert results", msgs)

    # ---- pack info ---------------------------------------------------------- #

    def pack_info(self):
        data = self.pack.data
        deps = data.setdefault("dependencies", {})
        cursor = 0
        while True:
            targets = [("versionId", "Version number", data), ("name", "Pack name", data),
                       ("summary", "Summary", data)] + [(k, f"dependency: {k}", deps) for k in list(deps)]
            rows = [f"{fit(label, 24)} {box.get(key, '')}" for key, label, box in targets]
            res = pick(self.scr, "Pack info", rows, hotkeys="bm", cursor=cursor,
                       help_line="Enter edit   b bump patch (1.2.3 -> 1.2.4)   m bump minor (1.2.3 -> 1.3.0)   Esc back")
            if res.key == "esc":
                return
            cursor = res.index or 0
            if res.key in "bm":
                m = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", str(data.get("versionId", "")))
                if not m:
                    show_text(self.scr, "Bump", ["versionId is not in MAJOR.MINOR.PATCH form; edit it by hand."])
                    continue
                a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
                data["versionId"] = f"{a}.{b + 1}.0" if res.key == "m" else f"{a}.{b}.{c + 1}"
                self.pack.dirty = True
            elif res.key == "enter" and res.index is not None:
                key, label, box = targets[res.index]
                new = prompt(self.scr, label, str(box.get(key, "")))
                if new is not None and new != str(box.get(key, "")):
                    box[key] = new
                    self.pack.dirty = True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def find_index(arg):
    if arg:
        return Path(arg)
    for cand in ("src/modrinth.index.json", "modrinth.index.json"):
        if Path(cand).is_file():
            return Path(cand)
    return Path("src/modrinth.index.json")


def run_cli(cmd, pack, api, newest):
    if cmd == "check":
        a = analyze(pack.files)
        print(f"{len(pack.files)} entries in index, {len(pack.local_jars())} local jar(s) in overrides/mods")
        for line in a.lines or ["index looks OK"]:
            print(("PROBLEM: " if a.problems else "") + line)
        return 1 if a.problems else 0
    if cmd == "convert":
        for m in convert_local(pack, api):
            print(m)
    for m in fix_index(pack):
        print(m)
    if cmd == "fix" and newest:
        plan, skipped = plan_keep_newest(pack, api)
        for line in describe_keep_newest(api, plan, skipped):
            print(line if isinstance(line, str) else line[0])
        print(f"removed {apply_keep_newest(pack, plan)} old entries")
    if pack.changed:
        pack.save()
        print(f"saved {pack.index_path}")
    else:
        print("nothing to change")
    return 0


def main():
    ap = argparse.ArgumentParser(description="TUI manager for a Modrinth modpack source tree.")
    ap.add_argument("command", nargs="?", default="tui", choices=["tui", "check", "fix", "convert"])
    ap.add_argument("--index", help="path to modrinth.index.json (default: src/modrinth.index.json)")
    ap.add_argument("--mods", help="path to overrides/mods (default: next to the index)")
    ap.add_argument("--newest", action="store_true", help="with 'fix': keep only the newest version of each mod")
    args = ap.parse_args()

    index_path = find_index(args.index)
    if not index_path.is_file():
        sys.exit(f"Can't find {index_path}. Run from the repo root or pass --index PATH.")
    mods_dir = Path(args.mods) if args.mods else index_path.parent / "overrides" / "mods"
    try:
        pack = Pack(index_path, mods_dir)
    except (OSError, ValueError) as e:
        sys.exit(f"Can't read {index_path}: {e}")
    api = Modrinth()

    if args.command != "tui":
        try:
            sys.exit(run_cli(args.command, pack, api, args.newest))
        except ApiError as e:
            sys.exit(f"Modrinth API error: {e}")

    if not sys.stdout.isatty():
        sys.exit("The TUI needs a terminal. Use 'check', 'fix' or 'convert' for non-interactive runs.")
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")

    def tui(scr):
        App(scr, pack, api).run()

    curses.wrapper(tui)


if __name__ == "__main__":
    main()