import subprocess

hashes = []

result = subprocess.run(["sha1sum", "src/overrides/mods/*"], stdout=subprocess.PIPE, text=True)
hashes = result.stdout.splitlines()

print("Hashes:")
for hash_line in hashes:
    print(hash_line)