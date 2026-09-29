import glob
import hashlib
import requests

hashes = []

for filename in glob.glob("src/overrides/mods/*"):
    with open(filename, "rb") as f:
        hashes.append(hashlib.sha1(f.read()).hexdigest())

print("Requesting URLs...")
# use https://api.modrinth.com/v2/version_file/{hash}

for hash_value in hashes:
    url = f"https://api.modrinth.com/v2/version_file/{hash_value}"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        print(f"Hash: {hash_value}, URL: {data.get("files")[0].get("url")}")
    else:
        print(f"Hash: {hash_value}, Error: {response.status_code}")
