import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

import requests

mods_directory = Path("src/overrides/mods")
index_path = Path("src/modrinth.index.json")


def dedupe_files(index):
    """Collapse index entries that share the same sha1, keeping the one whose
    filename matches the Modrinth download URL (falls back to the first seen)."""
    kept = {}
    order = []
    removed = []
    for entry in index["files"]:
        sha1 = entry.get("hashes", {}).get("sha1")
        if sha1 is None:
            order.append(entry)
            continue
        if sha1 not in kept:
            kept[sha1] = entry
            order.append(entry)
            continue

        current = kept[sha1]

        def matches_url(e):
            urls = e.get("downloads", [])
            if not urls:
                return False
            url_name = unquote(urls[0].rsplit("/", 1)[-1])
            return Path(e["path"]).name == url_name

        if matches_url(entry) and not matches_url(current):
            order[order.index(current)] = entry
            kept[sha1] = entry
            removed.append(current["path"])
        else:
            removed.append(entry["path"])

    index["files"] = order
    return removed


with index_path.open(encoding="utf-8") as index_file:
    index = json.load(index_file)

print("Requesting URLs...")
converted_mods = []
for mod_path in sorted(path for path in mods_directory.iterdir() if path.is_file()):
    content = mod_path.read_bytes()
    sha1_hash = hashlib.sha1(content).hexdigest()
    api_url = f"https://api.modrinth.com/v2/version_file/{sha1_hash}"

    try:
        response = requests.get(api_url, timeout=30)
    except requests.RequestException as error:
        print(f"Hash: {sha1_hash}, Request failed: {error}")
        continue

    if response.status_code == 200:
        data = response.json()
        api_file = next(
            (
                file
                for file in data.get("files", [])
                if file.get("hashes", {}).get("sha1") == sha1_hash
            ),
            None,
        )
        if api_file is None:
            print(f"Hash: {sha1_hash}, No matching file in API response")
            continue

        path = f"mods/{mod_path.name}"
        # Match by path first, then by sha1, so the same jar is never added twice
        entry = next(
            (file for file in index["files"] if file.get("path") == path),
            None,
        )
        if entry is None:
            entry = next(
                (
                    file
                    for file in index["files"]
                    if file.get("hashes", {}).get("sha1") == sha1_hash
                ),
                None,
            )
        if entry is None:
            entry = {
                "path": path,
                "hashes": {},
                "downloads": [],
                "fileSize": len(content),
            }
            index["files"].append(entry)

        entry["hashes"] = api_file.get("hashes", {"sha1": sha1_hash})
        entry["hashes"]["sha1"] = sha1_hash
        entry["downloads"] = [api_file["url"]]
        entry["fileSize"] = len(content)
        converted_mods.append((mod_path, sha1_hash))
        print(f"Hash: {sha1_hash}, URL: {api_file['url']}")
    else:
        print(f"Hash: {sha1_hash}, Error: {response.status_code}")

removed = dedupe_files(index)
for removed_path in removed:
    print(f"Removed duplicate index entry: {removed_path}")

with index_path.open("w", encoding="utf-8") as index_file:
    json.dump(index, index_file, indent="\t")
    index_file.write("\n")
print(f"Updated {index_path}")

for mod_path, sha1_hash in converted_mods:
    if mod_path.is_file() and hashlib.sha1(mod_path.read_bytes()).hexdigest() == sha1_hash:
        mod_path.unlink()
        print(f"Removed converted mod: {mod_path}")
