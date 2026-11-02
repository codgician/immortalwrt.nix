#!/usr/bin/env python3
"""List every source file a tree can download, keyed by the buildroot directory that needs it.

Run from the root of a configured tree (after `make prepare-tmpinfo`). For each source
Makefile, tools/ and toolchain/ directory this asks the buildroot itself what it would
download (`make -n -C <dir> download`), so URLs, hashes and git checkouts are exactly the ones
the build expects. Kernel tarballs come from target/linux/generic/kernel-<version>.

A few downloads depend on the architecture (prebuilt binaries). The dry run covers every directory
for x86_64 and aarch64; directories whose downloads differ between those two are arch-dependent
and are dry-run again for one target of every other architecture. A directory that downloads the
same for x86_64 and aarch64 but differs elsewhere would be missed; the build would then fail on
the missing file rather than download it.
"""

import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import sys

BASELINE_TARGETS = ["x86/64", "armsr/armv8"]
DOWNLOAD = re.compile(r'download\.pl "[^"]*" "([^"]+)" "([^"]*)" "([^"]*)"((?: "[^"]*")*)')
GIT_CLONE = re.compile(r"git clone --filter=blob:none +(\S+) (\S+) && \(cd \2 && git checkout (\S+)\)")
SUBMODULES = re.compile(
    r"git submodule update --init --recursive -- ([^&)]*?)\s*&& rm -rf \.git \.gitmodules"
)


def make(*args, check=True):
    """Run make in the FHS view; returns (exit status, stdout, stderr)."""
    cmd = ["owrt-fhs", os.environ["NIX_BUILD_TOP"], "make", *args]
    proc = subprocess.run(cmd, text=True, capture_output=True, env=MAKE_ENV)
    if check and proc.returncode != 0:
        sys.exit(f"{' '.join(cmd)} failed:\n{proc.stderr[-4000:]}")
    return proc.returncode, proc.stdout, proc.stderr


# The top-level Makefile exports these to every package build (include/toplevel.mk); some
# Makefiles, such as Go's, cannot even be parsed without them.
MAKE_ENV = dict(
    os.environ,
    HOST_OS=os.uname().sysname,
    HOST_ARCH=os.uname().machine,
    GNU_HOST_NAME=subprocess.run(["scripts/config.guess"], text=True, capture_output=True).stdout.strip(),
)


def configure(target):
    board, sub = target.split("/")
    conf = f"CONFIG_TARGET_{board}=y\nCONFIG_TARGET_{board}_{sub}=y\n".replace("-", "_")
    pathlib.Path(".config").write_text(conf)
    make("defconfig")


def expand(url, mirrors, filename):
    m = re.fullmatch(r"@([A-Z]+)(?:/(.*))?", url)
    if not m:
        return [url]
    name, path = m[1], m[2]
    if name in ("OPENWRT", "IMMORTALWRT"):
        return []  # appended for every file anyway
    paths = [path]
    if name == "KERNEL" and (v := re.match(r"linux-(\d+\.\d+(?:\.\d+)?)(?!-rc)", filename)):
        paths.append(f"{path}/longterm/v{v[1]}")
    urls = [f"{base}/{p}" for p in paths for base in mirrors.get(f"@{name}", [])]
    if name == "GITHUB":
        urls.append(f"https://raw.gitmirror.com/{path}")
    return urls


def parse(output, mirrors):
    entries = []
    for line in output.splitlines():
        m = DOWNLOAD.search(line)
        if not m:
            continue
        filename, sha256, url_file, urls = m[1], m[2], m[3], re.findall(r'"([^"]*)"', m[4])
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            continue  # PKG_HASH:=skip cannot be fetched reproducibly
        entry = {"file": filename, "sha256": sha256}
        if urls:
            name = url_file or filename
            entry["urls"] = [
                f"{u.rstrip('/')}/{name}" for url in urls for u in expand(url, mirrors, filename)
            ]
        elif g := GIT_CLONE.search(line):
            sub = SUBMODULES.search(line)
            entry["git"] = {
                "url": g[1],
                "subdir": g[2],
                "rev": g[3],
                "submodules": sub[1].split() if sub else "skip",
            }
        else:
            continue  # mirror-only file; nothing to fetch it from
        entries.append(entry)
    return entries


def dry_run(directory):
    status, out, err = make("-n", "-C", directory, "download", f"TOPDIR={os.getcwd()}", "V=s", check=False)
    if status and "No rule to make target 'download'" not in err:
        return directory, out, (err.strip().splitlines() or ["?"])[-1]
    return directory, out, None


def source_dirs():
    dirs = set()
    for line in pathlib.Path("tmp/.packageinfo").read_text(encoding="latin-1").splitlines():
        if line.startswith("Source-Makefile: "):
            dirs.add(
                os.path.relpath(os.path.realpath(line.split(": ", 1)[1].strip())).removesuffix("/Makefile")
            )
    for base in ("tools", "toolchain"):
        for makefile in pathlib.Path(base).rglob("Makefile"):
            dirs.add(str(makefile.parent))
    return sorted(dirs)


def kernels(mirrors):
    result = {}
    for path in pathlib.Path("target/linux/generic").glob("kernel-*"):
        text = path.read_text()
        for minor, patch in re.findall(r"^LINUX_VERSION-(\S+) = (\S*)", text, re.M):
            version = minor + patch
            sha = re.search(rf"^LINUX_KERNEL_HASH-{re.escape(version)} = ([0-9a-f]{{64}})", text, re.M)
            if not sha:
                continue
            major = version.split(".")[0]
            filename = f"linux-{version}.tar.xz"
            urls = expand(f"@KERNEL/linux/kernel/v{major}.x", mirrors, filename)
            result[version] = {"file": filename, "sha256": sha[1], "urls": [f"{u}/{filename}" for u in urls]}
    return result


def arch_targets(baseline):
    """One target with subtargets for every architecture not covered by the baseline targets."""
    arch_of, targets = {}, {}
    for block in pathlib.Path("tmp/.targetinfo").read_text(encoding="latin-1").split("\n@@\n"):
        target = re.search(r"^Target: (\S+/\S+)$", block, re.M)
        arch = re.search(r"^Target-Arch: (\S+)$", block, re.M)
        if target and arch:
            arch_of[target[1]] = arch[1]
            targets.setdefault(arch[1], target[1])
    covered = {arch_of.get(t) for t in baseline}
    return [t for arch, t in sorted(targets.items()) if arch not in covered]


def main():
    revision = sys.argv[1]
    mirrors = json.loads(pathlib.Path("scripts/projectsmirrors.json").read_text())
    dirs = source_dirs()
    found = {d: {} for d in dirs}
    failed = {}
    files_by_target = {}
    jobs = int(os.environ.get("NIX_BUILD_CORES") or 4)

    def scan(target, directories):
        configure(target)
        files = files_by_target.setdefault(target, {})
        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            for directory, output, error in pool.map(dry_run, directories):
                if error:
                    failed[directory] = error
                entries = parse(output, mirrors)
                files[directory] = {e["file"] for e in entries}
                for entry in entries:
                    found[directory].setdefault(entry["file"], entry)

    for target in BASELINE_TARGETS:
        scan(target, dirs)
    first, second = (files_by_target[t] for t in BASELINE_TARGETS)
    arch_dependent = [d for d in dirs if first.get(d) != second.get(d)]
    for target in arch_targets(BASELINE_TARGETS):
        scan(target, arch_dependent)
    for directory, error in sorted(failed.items()):
        print(f"sources.py: {directory}: {error}", file=sys.stderr)
    if failed:
        sys.exit(f"sources.py: the download dry run failed in {len(failed)} directories")
    sources = {
        "revision": revision,
        "mirrors": mirrors.get("@IMMORTALWRT", []) + mirrors.get("@OPENWRT", []),
        "kernel": kernels(mirrors),
        "dirs": {d: sorted(files.values(), key=lambda e: e["file"]) for d, files in found.items() if files},
    }
    json.dump(sources, sys.stdout, indent=1, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
