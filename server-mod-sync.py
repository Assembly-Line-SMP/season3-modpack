#!/usr/bin/env python3
"""
update-mods.py - the reverse of get-hashes.py.

Fetches the latest release of the Assembly Line SMP season 3 modpack from GitHub,
reads its modrinth.index.json, and syncs the *server-side* mods into a local mods folder:

  * client-only mods (env.server == "unsupported") are ignored completely
  * mods already present with the same hash are left alone
  * mods whose version changed are replaced (old file removed, new one downloaded)
  * mods that were not there are simply added

Old files are identified by hashing them and asking Modrinth which project they belong to,
so a renamed/updated jar is recognised as "the same mod, different version".
Files Modrinth doesn't know about (hand-made / third-party jars) are never touched.

Meant to be run on the server, not in the modpack repository.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

REPO = "Assembly-Line-SMP/season3-modpack"
DEFAULT_MODS_DIR = "./mods"
MODRINTH_API = "https://api.modrinth.com/v2"
USER_AGENT = "Assembly-Line-SMP/server-mod-updater (update-mods.py)"

# Same host whitelist the .mrpack spec / launchers use.
ALLOWED_HOSTS = {
    "cdn.modrinth.com",
    "github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT
if os.environ.get("GITHUB_TOKEN"):
    # Optional: avoids GitHub's anonymous rate limit. Only sent to api.github.com below.
    GITHUB_HEADERS = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
else:
    GITHUB_HEADERS = {}


# --------------------------------------------------------------------------- helpers
def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def confirm(question: str, assume_yes: bool, default: bool = False) -> bool:
    if assume_yes:
        return True
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{question} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# --------------------------------------------------------------------------- mods folder
def choose_mods_dir(arg: str | None, assume_yes: bool) -> Path:
    default = arg or DEFAULT_MODS_DIR
    if arg is None and not assume_yes:
        typed = input(f"Mods folder [{DEFAULT_MODS_DIR}]: ").strip()
        default = typed or DEFAULT_MODS_DIR

    mods_dir = Path(default).expanduser().resolve()
    print(f"Mods folder: {mods_dir}")

    if not mods_dir.exists():
        if not confirm("That folder does not exist. Create it?", assume_yes):
            sys.exit("Aborted.")
        mods_dir.mkdir(parents=True)
    elif not mods_dir.is_dir():
        sys.exit(f"{mods_dir} is not a directory.")
    else:
        jars = list(mods_dir.glob("*.jar"))
        print(f"Found {len(jars)} .jar file(s) in it.")

    if not confirm("Use this folder?", assume_yes):
        sys.exit("Aborted.")
    return mods_dir


# --------------------------------------------------------------------------- GitHub release
def fetch_index(repo: str, tag: str | None, local_mrpack: str | None):
    """Returns (release_name, index_dict)."""
    if local_mrpack:
        path = Path(local_mrpack)
        if path.suffix == ".json":
            return path.name, json.loads(path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(path) as z:
            return path.name, json.loads(z.read("modrinth.index.json"))

    url = f"https://api.github.com/repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest")
    print(f"Checking {url} ...")
    r = SESSION.get(url, headers=GITHUB_HEADERS, timeout=30)
    r.raise_for_status()
    release = r.json()
    name = release.get("name") or release["tag_name"]
    print(f"Release: {name} (tag {release['tag_name']}, published {release.get('published_at')})")

    assets = release.get("assets", [])
    mrpack = next((a for a in assets if a["name"].endswith(".mrpack")), None)
    raw_index = next((a for a in assets if a["name"] == "modrinth.index.json"), None)

    if mrpack:
        print(f"Downloading {mrpack['name']} ({human_size(mrpack['size'])}) ...")
        data = SESSION.get(mrpack["browser_download_url"], timeout=120)
        data.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(data.content)) as z:
            return name, json.loads(z.read("modrinth.index.json"))
    if raw_index:
        data = SESSION.get(raw_index["browser_download_url"], timeout=60)
        data.raise_for_status()
        return name, data.json()

    names = ", ".join(a["name"] for a in assets) or "none"
    sys.exit(f"No .mrpack or modrinth.index.json asset in that release (assets: {names}).")


# --------------------------------------------------------------------------- planning
def wanted_server_mods(index: dict, skip_optional: bool):
    wanted, client_only, optional_skipped = [], 0, 0
    for f in index.get("files", []):
        path = f.get("path", "")
        if not path.startswith("mods/"):
            continue  # resourcepacks, shaderpacks, config, ...
        name = Path(path).name
        if name != path[len("mods/"):]:
            print(f"  ! skipping suspicious path: {path}")
            continue

        server_env = (f.get("env") or {}).get("server", "required")
        if server_env == "unsupported":
            client_only += 1
            continue
        if server_env == "optional" and skip_optional:
            optional_skipped += 1
            continue

        urls = [u for u in f.get("downloads", []) if urlparse(u).hostname in ALLOWED_HOSTS]
        if not urls:
            print(f"  ! {name}: no download URL on an allowed host, skipping")
            continue

        m = re.search(r"/data/([^/]+)/versions/", urls[0])
        wanted.append({
            "name": name,
            "urls": urls,
            "sha1": f["hashes"]["sha1"],
            "sha512": f["hashes"].get("sha512"),
            "size": f.get("fileSize", 0),
            "project_id": m.group(1) if m else None,
            "optional": server_env == "optional",
        })
    return wanted, client_only, optional_skipped


def identify_local(mods_dir: Path):
    """Hash every local jar and ask Modrinth which project each one is. Returns list of dicts."""
    jars = sorted(p for p in mods_dir.glob("*.jar") if p.is_file())
    local = [{"path": p, "sha1": file_hash(p, "sha1"), "project_id": None} for p in jars]
    if not local:
        return local

    print(f"Identifying {len(local)} existing mod(s) via Modrinth ...")
    by_hash = {}
    hashes = [m["sha1"] for m in local]
    for i in range(0, len(hashes), 500):
        r = SESSION.post(
            f"{MODRINTH_API}/version_files",
            json={"hashes": hashes[i:i + 500], "algorithm": "sha1"},
            timeout=60,
        )
        r.raise_for_status()
        by_hash.update(r.json())
    for m in local:
        version = by_hash.get(m["sha1"])
        if version:
            m["project_id"] = version["project_id"]
    return local


def make_plan(wanted, local, mods_dir: Path):
    local_by_sha = {m["sha1"]: m for m in local}
    local_by_project = {}
    for m in local:
        if m["project_id"]:
            local_by_project.setdefault(m["project_id"], []).append(m)

    up_to_date, add, update = [], [], []
    claimed = set()  # local paths that belong to a wanted mod

    for w in wanted:
        if w["sha1"] in local_by_sha:
            up_to_date.append(w)
            claimed.add(local_by_sha[w["sha1"]]["path"])
            continue

        olds = list(local_by_project.get(w["project_id"], [])) if w["project_id"] else []
        # Same filename but unknown/different content -> it gets overwritten too.
        same_name = mods_dir / w["name"]
        if same_name.exists() and all(o["path"] != same_name for o in olds):
            olds.append({"path": same_name, "sha1": None, "project_id": None})

        if olds:
            update.append((w, olds))
            claimed.update(o["path"] for o in olds)
        else:
            add.append(w)

    extras = [m for m in local if m["path"] not in claimed]
    return up_to_date, add, update, extras


# --------------------------------------------------------------------------- applying
def download(w, mods_dir: Path) -> Path:
    tmp = mods_dir / (w["name"] + ".part")
    last_error = None
    for url in w["urls"]:
        for attempt in range(3):
            try:
                with SESSION.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    sha1, sha512 = hashlib.sha1(), hashlib.sha512()
                    with tmp.open("wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            f.write(chunk)
                            sha1.update(chunk)
                            sha512.update(chunk)
                if sha1.hexdigest() != w["sha1"]:
                    raise ValueError("sha1 mismatch")
                if w["sha512"] and sha512.hexdigest() != w["sha512"]:
                    raise ValueError("sha512 mismatch")
                return tmp
            except (requests.RequestException, ValueError) as e:
                last_error = e
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"{w['name']}: {last_error}")


def apply_plan(add, update, remove, mods_dir: Path, jobs: int, backup_dir: Path | None):
    todo = [(w, []) for w in add] + update
    failures = []

    def retire(path: Path):
        if not path.exists():
            return
        if backup_dir:
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(backup_dir / path.name))
        else:
            path.unlink()

    print(f"\nDownloading {len(todo)} mod(s) with {jobs} worker(s) ...")
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(download, w, mods_dir): (w, olds) for w, olds in todo}
        for fut in as_completed(futures):
            w, olds = futures[fut]
            try:
                tmp = fut.result()
            except Exception as e:  # noqa: BLE001
                failures.append(str(e))
                print(f"  FAILED  {e}")
                continue
            # New file is verified; now it's safe to retire the old versions.
            for old in olds:
                if old["path"].name != w["name"]:
                    retire(old["path"])
            final = mods_dir / w["name"]
            if final.exists():
                retire(final)
            os.replace(tmp, final)
            print(f"  OK      {w['name']}")

    for m in remove:
        retire(m["path"])
        print(f"  REMOVED {m['path'].name}")
    return failures


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Sync server-side mods from the latest modpack release.")
    ap.add_argument("--mods-dir", help=f"mods folder (default: {DEFAULT_MODS_DIR}, still asks to confirm)")
    ap.add_argument("--repo", default=REPO, help=f"GitHub repo (default: {REPO})")
    ap.add_argument("--tag", help="use a specific release tag instead of the latest")
    ap.add_argument("--mrpack", help="use a local .mrpack / modrinth.index.json instead of GitHub")
    ap.add_argument("--skip-optional", action="store_true", help="also skip mods marked server-optional")
    ap.add_argument("--remove-extra", action="store_true",
                    help="remove Modrinth mods that are in the folder but not in the pack "
                         "(unknown jars are never touched)")
    ap.add_argument("--backup", action="store_true",
                    help="move replaced/removed jars to ./mods-backup/<timestamp> instead of deleting")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="parallel downloads (default 4)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()

    mods_dir = choose_mods_dir(args.mods_dir, args.yes)

    release_name, index = fetch_index(args.repo, args.tag, args.mrpack)
    print(f"Modpack: {index.get('name', '?')} {index.get('versionId', '')}")

    wanted, client_only, optional_skipped = wanted_server_mods(index, args.skip_optional)
    local = identify_local(mods_dir)
    up_to_date, add, update, extras = make_plan(wanted, local, mods_dir)

    removable = [m for m in extras if m["project_id"]] if args.remove_extra else []
    unknown = [m for m in extras if not m["project_id"]]
    kept_known = [m for m in extras if m["project_id"] and not args.remove_extra]

    # ---- summary
    print("\n=== Plan ===")
    print(f"Ignored client-only mods : {client_only}")
    if optional_skipped:
        print(f"Skipped optional mods    : {optional_skipped}")
    print(f"Already up to date       : {len(up_to_date)}")
    print(f"To update                : {len(update)}")
    for w, olds in update:
        print(f"  ~ {', '.join(o['path'].name for o in olds)}  ->  {w['name']}")
    print(f"To add                   : {len(add)}")
    for w in add:
        print(f"  + {w['name']}" + ("  (optional)" if w["optional"] else ""))
    if removable:
        print(f"To remove (not in pack)  : {len(removable)}")
        for m in removable:
            print(f"  - {m['path'].name}")
    if kept_known:
        print(f"Not in pack, kept        : {len(kept_known)} (use --remove-extra to delete)")
        for m in kept_known:
            print(f"  ? {m['path'].name}")
    if unknown:
        print(f"Unknown to Modrinth, kept: {len(unknown)}")
        for m in unknown:
            print(f"  ? {m['path'].name}")
    total = sum(w["size"] for w in add) + sum(w["size"] for w, _ in update)
    print(f"Download size            : {human_size(total)}")

    if not (add or update or removable):
        print("\nNothing to do.")
        return
    if args.dry_run:
        print("\nDry run - no changes made.")
        return
    if not confirm("\nApply these changes?", args.yes):
        sys.exit("Aborted.")

    backup_dir = None
    if args.backup:
        backup_dir = mods_dir.parent / "mods-backup" / datetime.now().strftime("%Y%m%d-%H%M%S")

    failures = apply_plan(add, update, removable, mods_dir, max(1, args.jobs), backup_dir)

    print("\nDone." if not failures else f"\nFinished with {len(failures)} failure(s):")
    for f in failures:
        print(f"  - {f}")
    if backup_dir and backup_dir.exists():
        print(f"Old files saved in {backup_dir}")
    print("Restart the server for the changes to take effect.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")