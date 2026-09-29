import hashlib
import json
from pathlib import Path

import requests

mods_directory = Path("src/overrides/mods")
index_path = Path("src/modrinth.index.json")

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
        entry = next(
            (file for file in index["files"] if file.get("path") == path),
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

with index_path.open("w", encoding="utf-8") as index_file:
    json.dump(index, index_file, indent="\t")
    index_file.write("\n")
print(f"Updated {index_path}")

for mod_path, sha1_hash in converted_mods:
    if mod_path.is_file() and hashlib.sha1(mod_path.read_bytes()).hexdigest() == sha1_hash:
        mod_path.unlink()
        print(f"Removed converted mod: {mod_path}")
