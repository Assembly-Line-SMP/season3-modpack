#!/usr/bin/python3

# Simple script to merge Modrinth index files

import json
import os

fcurrent = open("./src/modrinth.index.json", "r")
current_data = json.load(fcurrent)
fcurrent.close()

fnew = open("modrinth.index.json", "r")
new_data = json.load(fnew)
fnew.close()

for new_file in new_data["files"]:
    if new_file not in current_data["files"]:
        current_data["files"].append(new_file)

current_data["versionId"] = new_data["versionId"]

fmerged = open("./src/modrinth.index.json", "w")
json.dump(current_data, fmerged, indent="\t")
fmerged.close()
os.remove("modrinth.index.json")