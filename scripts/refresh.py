#!/usr/bin/env python3
"""Refresh channel locks, metadata, sources and vendored Go modules.

For each channel, resolve the upstream revision, pin every feed listed in the tree's
feeds.conf.default, and prefetch all sources. Then build every channel's tree and metadata, and
hash the Go modules of the packages listed in vendor-packages.json: a changed lock is only kept
when all of that succeeds, otherwise the channel keeps its last good state.

Release channels follow the newest tag of their series. New series are picked up automatically:
channels are created when upstream opens an openwrt-XX.YY branch and when it tags vXX.YY.Z of a
new series.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
CHANNELS = ROOT / "channels"
IMMORTALWRT = {"owner": "immortalwrt", "repo": "immortalwrt"}
HISTORY_SEARCH_LIMIT = 300
GENERATED = ("metadata.json", "sources.json", "vendor.json")


class RefreshError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(cmd, cwd=None, stdin=None):
    proc = subprocess.run(cmd, cwd=cwd, stdin=stdin, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RefreshError(f"`{' '.join(map(str, cmd))}` failed:\n{proc.stderr.strip()}")
    return proc.stdout


def load_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def github_url(owner, repo):
    return f"https://github.com/{owner}/{repo}.git"


def ls_remote(url, *patterns):
    refs = {}
    for line in run(["git", "ls-remote", url, *patterns]).splitlines():
        sha, ref = line.split("\t")
        refs[ref] = sha
    return refs


def fetch_spec(url, rev):
    m = re.fullmatch(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?", url)
    if m:
        return {"type": "github", "owner": m[1], "repo": m[2], "rev": rev}
    return {"type": "git", "url": url, "rev": rev}


def prefetch(fetch):
    """Return (fetch with narHash, lastModified, store path)."""
    if fetch["type"] == "github":
        ref = f"github:{fetch['owner']}/{fetch['repo']}/{fetch['rev']}"
    else:
        ref = f"git+{fetch['url']}?rev={fetch['rev']}"
    info = json.loads(run(["nix", "flake", "prefetch", "--json", ref]))
    locked = dict(fetch, narHash=info["hash"])
    return locked, info["locked"].get("lastModified"), pathlib.Path(info["storePath"])


def channel_names():
    return sorted(p.parent.name for p in CHANNELS.glob("*/channel.json"))


def snapshot(name):
    """The channel's committed state: its lock and generated files (None when missing)."""
    files = {
        f: (CHANNELS / name / f).read_bytes() if (CHANNELS / name / f).exists() else None for f in GENERATED
    }
    return {"lock": load_json(CHANNELS / name / "lock.json"), "files": files}


def stage(path, content):
    """Write (or with None, remove) a file and stage the change."""
    if content is None:
        path.unlink(missing_ok=True)
        run(["git", "rm", "--cached", "--quiet", "--ignore-unmatch", str(path)], cwd=ROOT)
    else:
        path.write_bytes(content)
        run(["git", "add", str(path)], cwd=ROOT)


def restore(name, old, created):
    """Put back a channel's last good state; drop the channel if this run created it."""
    if name in created:
        shutil.rmtree(CHANNELS / name, ignore_errors=True)
        run(["git", "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", str(CHANNELS / name)], cwd=ROOT)
        return
    lock = old["lock"]
    stage(
        CHANNELS / name / "lock.json",
        None if lock is None else (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode(),
    )
    for f, content in old["files"].items():
        stage(CHANNELS / name / f, content)


# ---------------------------------------------------------------------------- channel discovery


def ensure_release_channels():
    """Create channels for the newest ImmortalWrt release branch and release series.

    Returns the names of the channels created.
    """
    url = github_url(**IMMORTALWRT)
    refs = ls_remote(url, "refs/heads/openwrt-*", "refs/tags/v*")
    branches = [m[1] for r in refs if (m := re.fullmatch(r"refs/heads/openwrt-(\d+\.\d+)", r))]
    releases = [m[2] for r in refs if (m := re.fullmatch(r"refs/tags/v((\d+\.\d+)\.\d+)", r))]
    wanted = {}
    if branches:
        series = max(branches, key=version_key)
        wanted[f"immortalwrt-{series}-snapshot"] = {
            "description": f"ImmortalWrt {series} release branch snapshot (openwrt-{series})",
            "follow": {"branch": f"openwrt-{series}"},
            "series": series,
            "upstream": IMMORTALWRT,
            "version": f"{series}-SNAPSHOT",
        }
    if releases:
        series = max(releases, key=version_key)
        wanted[f"immortalwrt-{series}"] = {
            "description": f"ImmortalWrt {series} stable releases (newest v{series}.x tag)",
            "follow": {"tagSeries": series},
            "series": series,
            "upstream": IMMORTALWRT,
        }
    created = []
    for name, spec in wanted.items():
        path = CHANNELS / name / "channel.json"
        if not path.exists():
            write_json(path, spec)
            created.append(name)
            log(f"created channel {name}")
    return created


# ---------------------------------------------------------------------------- resolution


def resolve_src(spec):
    """Return (url, ref, rev, version) for the channel's upstream."""
    url = github_url(spec["upstream"]["owner"], spec["upstream"]["repo"])
    follow = spec["follow"]
    if "branch" in follow:
        ref = f"refs/heads/{follow['branch']}"
        refs = ls_remote(url, ref)
        if ref not in refs:
            raise RefreshError(f"{url}: branch {follow['branch']} not found")
        return url, follow["branch"], refs[ref], spec["version"]
    series = follow["tagSeries"]
    tags = {}
    for ref, sha in ls_remote(url, f"refs/tags/v{series}.*").items():
        m = re.fullmatch(rf"refs/tags/v({re.escape(series)}\.\d+)(\^\{{\}})?", ref)
        if m and (m[2] or m[1] not in tags):  # peeled entry wins for annotated tags
            tags[m[1]] = sha
    if not tags:
        raise RefreshError(f"{url}: no v{series}.x release tag")
    version = max(tags, key=version_key)
    return url, f"v{version}", tags[version], version


def parse_feeds(path):
    feeds = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        method, *tokens = line.split()
        if method not in ("src-git", "src-git-full"):
            raise RefreshError(f"unsupported feed in feeds.conf.default: {line}")
        flags = dict(t[2:].partition("=")[::2] for t in tokens if t.startswith("--"))
        args = [t for t in tokens if not t.startswith("--")]
        if set(flags) - {"directory"} or len(args) != 2:
            raise RefreshError(f"unsupported feed line: {line}")
        name, location = args
        m = re.fullmatch(r"([^;^]+)(?:([;^])(.+))?", location)
        feeds.append(
            {"name": name, "url": m[1], "sep": m[2], "ref": m[3], "directory": flags.get("directory")}
        )
    return feeds


def feed_patches(src_path, spec, feed):
    if "feedPatches" not in spec:
        return []
    root = src_path / spec["feedPatches"]
    base = root / feed
    if not base.is_dir():
        return []
    # same order as `find | LC_ALL=C sort` in lib/tree.nix
    return sorted(base.rglob("*.patch"), key=lambda p: str(p.relative_to(root)).encode())


def apply_patches(work, patches):
    """Apply patches in a writable checkout; already-present changes count as applied."""
    for patch in patches:
        with open(patch) as f:
            if subprocess.run(["git", "apply"], cwd=work, stdin=f, capture_output=True).returncode == 0:
                continue
        with open(patch) as f:
            reverse = subprocess.run(
                ["git", "apply", "--reverse", "--check"], cwd=work, stdin=f, capture_output=True
            )
        if reverse.returncode != 0:
            return False
    return True


def patches_satisfied(tree, patches):
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp) / "feed"
        shutil.copytree(tree, work, symlinks=True)
        run(["chmod", "-R", "u+w", str(work)])
        return apply_patches(work, patches)


def newest_patchable(url, start, patches):
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, str(repo)])
        revs = run(
            ["git", "rev-list", "--first-parent", f"--max-count={HISTORY_SEARCH_LIMIT}", start], cwd=repo
        ).split()
        for rev in revs:
            run(["git", "checkout", "--quiet", "--force", rev], cwd=repo)
            run(["git", "clean", "-qfdx"], cwd=repo)
            if apply_patches(repo, patches):
                return rev
    raise RefreshError(f"{url}: no commit among the last {HISTORY_SEARCH_LIMIT} accepts the feed patches")


class History:
    """Git history of upstream repositories, for what the buildroot derives from `.git`.

    Trees are built without `.git`, where the buildroot falls back to build-time values. Instead,
    the lock records what a plain checkout would give: the REVISION string (scripts/getver.sh),
    base-files' release (its commit count) and LuCI package versions (luci.mk's findrev)."""

    def __init__(self, tmp):
        self.tmp = pathlib.Path(tmp)
        self.clones = {}

    def clone(self, url, rev):
        if url not in self.clones:
            path = self.tmp / f"clone{len(self.clones)}.git"
            run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", url, str(path)])
            self.clones[url] = path
        repo = self.clones[url]
        if subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"], cwd=repo).returncode != 0:
            run(["git", "fetch", "--quiet", "--filter=blob:none", "origin", rev], cwd=repo)
        return repo

    def revision(self, url, rev, src_path):
        shipped = src_path / "version"
        if shipped.is_file() and shipped.read_text().strip():
            return shipped.read_text().strip()  # release tags carry their official revision
        repo = self.clone(url, rev)
        getver = src_path / "scripts" / "getver.sh"
        m = re.search(r"REBOOT=([0-9a-f]{40})", getver.read_text()) if getver.exists() else None
        span = f"{m[1]}..{rev}" if m else rev
        count = run(["git", "rev-list", "--count", span], cwd=repo).strip()
        short = run(["git", "rev-parse", "--short=12", rev], cwd=repo).strip()
        return f"r{count}-{short}"

    def commit_count(self, url, rev, path):
        """rules.mk's COMMITCOUNT for a package directory."""
        return run(["git", "rev-list", "--count", rev, "--", path], cwd=self.clone(url, rev)).strip()

    def luci_versions(self, url, rev, feed_path):
        """luci.mk's findrev for every LuCI package of a feed, keyed by path below the feed root."""
        versions = {}
        for makefile in sorted(feed_path.rglob("Makefile")):
            if not re.search(r"^include .*/luci\.mk\s*$", makefile.read_text(errors="replace"), re.M):
                continue
            repo = self.clone(url, rev)
            rel = makefile.parent.relative_to(feed_path).as_posix()

            def findrev(*pathspec):
                out = run(
                    ["git", "log", "-1", "--format=%ct %h", "--abbrev=7", rev, "--", *pathspec], cwd=repo
                )
                if not out.strip():
                    return "0"
                ts, short = out.split()
                day = datetime.datetime.fromtimestamp(int(ts), datetime.UTC).strftime("%y.%j")
                return f"{day}.{int(ts) % 86400:05d}~{short}"

            po = "po" if rel == "." else f"{rel}/po"
            versions[rel] = {"src": findrev(rel, f":(exclude){po}"), "po": findrev(po)}
        return versions


def resolve_channel(name, history):
    """Return the channel's new lock and notes about held-back feeds."""
    spec = load_json(CHANNELS / name / "channel.json")
    url, ref, rev, version = resolve_src(spec)
    src_fetch, src_mtime, src_path = prefetch(fetch_spec(url, rev))
    notes = []
    feeds = []
    luci = {}
    for feed in parse_feeds(src_path / "feeds.conf.default"):
        if feed["sep"] == "^":
            frev, pinned_by = feed["ref"], "tree"
        else:
            head = f"refs/heads/{feed['ref']}" if feed["sep"] == ";" else "HEAD"
            refs = ls_remote(feed["url"], head)
            if head not in refs:
                raise RefreshError(f"feed {feed['name']}: {feed['url']} has no {head}")
            frev, pinned_by = refs[head], "branch" if feed["sep"] == ";" else "default-branch"
        fetch, _, path = prefetch(fetch_spec(feed["url"], frev))
        patches = feed_patches(src_path, spec, feed["name"])
        if patches and not patches_satisfied(path, patches):
            rejected, frev = frev, newest_patchable(feed["url"], frev, patches)
            fetch, _, path = prefetch(fetch_spec(feed["url"], frev))
            notes.append(f"feed {feed['name']} pinned to {frev[:12]}: patches reject {rejected[:12]}")
        entry = {"name": feed["name"], "fetch": fetch, "pinnedBy": pinned_by}
        if feed["sep"] == ";":
            entry["ref"] = feed["ref"]
        if feed["directory"]:
            entry["directory"] = feed["directory"]
        feeds.append(entry)
        # scripts/feeds checks a feed out to feeds/<--directory> when given, else feeds/<name>
        checkout = f"feeds/{feed['directory'] or feed['name']}"
        for rel, v in history.luci_versions(feed["url"], frev, path).items():
            luci[checkout if rel == "." else f"{checkout}/{rel}"] = v
    lock = {
        "version": version,
        "revision": history.revision(url, rev, src_path),
        "src": {"ref": ref, "lastModified": src_mtime, "fetch": src_fetch},
        "feeds": feeds,
        "packageVersions": {
            "baseFilesRelease": history.commit_count(url, rev, "package/base-files"),
            "luci": luci,
        },
    }
    return lock, notes


# ---------------------------------------------------------------------------- metadata, vendoring


def build_metadata(name):
    """Build the channel's metadata.json and sources.json; returns the output directory."""
    attr = f'.#"{name}-metadata"'
    proc = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths", attr], cwd=ROOT, text=True, capture_output=True
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
        raise RefreshError(f"building {attr} failed:\n{tail}")
    return pathlib.Path(proc.stdout.strip())


def go_modules_hash(name, directory):
    """Hash of a Go package's modules: build the probe (empty hash) and read what Nix got."""
    attr = f'.#legacyPackages.x86_64-linux.channels."{name}".goModulesProbe."{directory}"'
    proc = subprocess.run(["nix", "build", "--no-link", attr], cwd=ROOT, text=True, capture_output=True)
    # only the modules derivation's mismatch counts; a source fetch can fail the same way
    m = re.search(r"-go-modules\.drv':\s+specified:\s+\S+\s+got:\s+(sha256-[A-Za-z0-9+/]+=*)", proc.stderr)
    if not m:
        tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
        raise RefreshError(f"hashing the Go modules of {directory} failed:\n{tail}")
    return m[1]


def patches_digest(name, directory):
    """Digest of the patches lib/vendor.nix applies before downloading modules."""
    tree = run(["nix", "build", "--no-link", "--print-out-paths", f'.#"{name}-tree"'], cwd=ROOT).strip()
    digest = hashlib.sha256()
    for patch in sorted(pathlib.Path(tree, directory, "patches").glob("*.patch")):
        digest.update(patch.name.encode() + b"\0" + patch.read_bytes() + b"\0")
    return digest.hexdigest()


def vendor(name, old):
    """vendor.json for a channel: Go module hashes of the packages in vendor-packages.json.

    A hash is reused while the package's source file and patches are unchanged."""
    metadata = load_json(CHANNELS / name / "metadata.json")
    sources = load_json(CHANNELS / name / "sources.json")
    result = {}
    for package in load_json(ROOT / "vendor-packages.json")["go"]:
        info = metadata["packages"].get(package)
        if info is None:
            continue  # not in this channel
        directory = info["source"]
        if not metadata["sources"].get(directory, {}).get("go"):
            raise RefreshError(f"{package} (vendor-packages.json) is not a Go package in {name}")
        entries = sources["dirs"].get(directory, [])
        if len(entries) != 1:
            raise RefreshError(f"{directory} has {len(entries)} source files, cannot tell which holds go.mod")
        key = {"sha256": entries[0]["sha256"], "patches": patches_digest(name, directory)}
        if all(old.get(directory, {}).get(k) == v for k, v in key.items()):
            result[directory] = old[directory]
        else:
            log(f"{name}: hashing Go modules of {directory}")
            result[directory] = dict(key, hash=go_modules_hash(name, directory))
    return result


def changes(old, new):
    if old is None:
        return "new"
    parts = []
    if old["src"]["fetch"]["rev"] != new["src"]["fetch"]["rev"]:
        parts.append(f"src {old['src']['fetch']['rev'][:12]}→{new['src']['fetch']['rev'][:12]}")
    before = {f["name"]: f["fetch"]["rev"] for f in old["feeds"]}
    after = {f["name"]: f["fetch"]["rev"] for f in new["feeds"]}
    moved = sorted(n for n in after if before.get(n) != after[n])
    removed = sorted(set(before) - set(after))
    if moved:
        parts.append("feeds " + ", ".join(moved))
    if removed:
        parts.append("removed " + ", ".join(removed))
    return "; ".join(parts) or "unchanged"


def main():
    parser = argparse.ArgumentParser(prog="refresh", description=__doc__.splitlines()[0])
    parser.add_argument("channels", nargs="*", help="channels to refresh (default: all)")
    parser.add_argument("--summary", type=pathlib.Path, help="append a Markdown summary here")
    args = parser.parse_args()

    created = ensure_release_channels()
    unknown = sorted(set(args.channels) - set(channel_names()))
    if unknown:
        parser.error(f"unknown channels: {', '.join(unknown)}")

    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        history = History(tmp)
        for name in args.channels or channel_names():
            log(f"resolving {name}")
            r = results[name] = {"old": snapshot(name)}
            try:
                r["lock"], r["notes"] = resolve_channel(name, history)
            except RefreshError as e:
                r["error"] = str(e)
                if name in created:
                    restore(name, r["old"], created)

    # Write candidate locks, then build every resolved channel: this validates new locks as well
    # as unchanged ones against today's flake inputs.
    for name, r in results.items():
        if "error" in r or r["lock"] == r["old"]["lock"]:
            continue
        write_json(CHANNELS / name / "lock.json", r["lock"])
        run(["git", "add", str(CHANNELS / name)], cwd=ROOT)

    resolved = [name for name, r in results.items() if "error" not in r]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(resolved))) as pool:
        futures = {pool.submit(build_metadata, name): name for name in resolved}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            r = results[name]
            try:
                out = future.result()
                for f in ("metadata.json", "sources.json"):
                    stage(CHANNELS / name / f, (out / f).read_bytes())
                old_vendor = json.loads(r["old"]["files"]["vendor.json"] or b"{}")
                stage(
                    CHANNELS / name / "vendor.json",
                    (json.dumps(vendor(name, old_vendor), indent=2, sort_keys=True) + "\n").encode(),
                )
                updated = [f for f in GENERATED if (CHANNELS / name / f).read_bytes() != r["old"]["files"][f]]
                if updated:
                    log(f"{name}: updated {', '.join(updated)}")
            except RefreshError as e:
                r["error"] = str(e)
                restore(name, r["old"], created)

    rows = ["| channel | status | version | revision | changes |", "|---|---|---|---|---|"]
    for name, r in sorted(results.items()):
        failed = "error" in r
        lock = (r["old"]["lock"] if failed else r["lock"]) or {}
        change = "" if failed else changes(r["old"]["lock"], r["lock"])
        rows.append(
            f"| {name} | {'failed' if failed else 'ok'} | {lock.get('version', '')} | "
            f"{lock.get('revision', '')} | {change} |"
        )
    notes = [f"- **{n}**: {note}" for n, r in sorted(results.items()) for note in r.get("notes", [])]
    errors = [f"### {n}\n\n```\n{r['error']}\n```" for n, r in sorted(results.items()) if "error" in r]
    summary = "\n".join(["## Channel refresh", "", *rows, "", *notes, "", *errors]) + "\n"
    print(summary)
    if args.summary:
        with args.summary.open("a") as f:
            f.write(summary)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
