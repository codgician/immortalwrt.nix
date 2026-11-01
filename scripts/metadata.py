#!/usr/bin/env python3
"""Turn the buildroot scan (tmp/.packageinfo, tmp/.targetinfo) into channel metadata JSON.

Output is one entry per line with sorted keys so the daily refresh produces small git diffs.
"""

import json
import sys


def stanzas(path):
    with open(path, encoding="latin-1") as f:
        for block in f.read().split("\n@@\n"):
            fields = {}
            for line in block.splitlines():
                key, sep, value = line.partition(": ")
                if sep and key and " " not in key and key not in fields:
                    fields[key] = value.strip()
            yield fields


def packages(path):
    result, source = {}, None
    for st in stanzas(path):
        if "Source-Makefile" in st:
            source = st["Source-Makefile"].removesuffix("/Makefile")
        name = st.get("Package")
        if not name:
            continue
        entry = {"source": source}
        provides = st.get("Provides", "").split()
        if provides:
            entry["provides"] = sorted(set(provides))
        result[name] = entry
    return result


def targets(path):
    result, current = {}, None
    for st in stanzas(path):
        if "Target" in st:
            current = st["Target"]
            result[current] = {
                "arch": st.get("Target-Arch-Packages", ""),
                "linux": st.get("Linux-Version", ""),
                "profiles": [],
            }
        profile = st.get("Target-Profile", "")
        if current and profile.startswith("DEVICE_"):
            result[current]["profiles"].append(profile.removeprefix("DEVICE_"))
    return {
        name: dict(info, profiles=sorted(set(info["profiles"])))
        for name, info in result.items()
        if info["profiles"]
    }


def dump_section(name, entries, last=False):
    lines = [
        f"  {json.dumps(key)}: {json.dumps(value, sort_keys=True)}" for key, value in sorted(entries.items())
    ]
    return f'  "{name}": {{\n' + ",\n".join(f"  {line}" for line in lines) + "\n  }" + ("" if last else ",")


def main():
    packageinfo, targetinfo, revision = sys.argv[1:4]
    print("{")
    print(f'  "revision": {json.dumps(revision)},')
    print(dump_section("targets", targets(targetinfo)))
    print(dump_section("packages", packages(packageinfo), last=True))
    print("}")


if __name__ == "__main__":
    main()
