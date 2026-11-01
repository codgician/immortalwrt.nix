#!/usr/bin/env python3
"""Refresh channel locks and metadata.

For each channel, resolve the upstream revision, pin every feed listed in the tree's
feeds.conf.default, and prefetch all sources. Then build every channel's tree and metadata: a
changed lock is only kept when that build succeeds, otherwise the last good lock stays.

Release channels follow the newest tag of their series whose official binaries
nix-immortalwrt-imagebuilder has pinned. New series are picked up automatically: channels are
created when upstream opens an openwrt-XX.YY branch and when a vXX.YY.Z of a new series is pinned.
"""

from __future__ import annotations

import argparse
import concurrent.futures
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


def published_releases():
    """Releases whose official ImageBuilder the image backend has pinned (flake input)."""
    out = run(["nix", "eval", "--json", ".#legacyPackages.x86_64-linux.binaryReleases"], cwd=ROOT)
    return set(json.loads(out))


def channel_names():
    return sorted(p.parent.name for p in CHANNELS.glob("*/channel.json"))


def restore(name, old, created):
    """Put back a channel's last good lock; drop the channel if this run created it."""
    if name in created:
        shutil.rmtree(CHANNELS / name, ignore_errors=True)
        run(["git", "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", str(CHANNELS / name)], cwd=ROOT)
        return
    lock = CHANNELS / name / "lock.json"
    if old is None:
        lock.unlink(missing_ok=True)
        run(["git", "rm", "--cached", "--quiet", "--ignore-unmatch", str(lock)], cwd=ROOT)
    else:
        write_json(lock, old)
        run(["git", "add", str(lock)], cwd=ROOT)


# ---------------------------------------------------------------------------- channel discovery


def ensure_release_channels(published):
    """Create channels for the newest ImmortalWrt release branch and published release series.

    Returns the names of the channels created.
    """
    url = github_url(**IMMORTALWRT)
    refs = ls_remote(url, "refs/heads/openwrt-*", "refs/tags/v*")
    branches = [m[1] for r in refs if (m := re.fullmatch(r"refs/heads/openwrt-(\d+\.\d+)", r))]
    releases = [
        m[2] for r in refs if (m := re.fullmatch(r"refs/tags/v((\d+\.\d+)\.\d+)", r)) and m[1] in published
    ]
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
            "description": f"ImmortalWrt {series} stable releases (newest published v{series}.x tag)",
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


def resolve_src(spec, published):
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
    ready = [v for v in tags if v in published]
    if not ready:
        raise RefreshError(f"{url}: no published v{series}.x release (tags: {', '.join(sorted(tags))})")
    version = max(ready, key=version_key)
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


class Revisions:
    """REVISION strings as scripts/getver.sh prints them for a plain checkout (r<count>-<hash>)."""

    def __init__(self, tmp):
        self.tmp = pathlib.Path(tmp)
        self.clones = {}

    def clone(self, url):
        if url not in self.clones:
            path = self.tmp / f"clone{len(self.clones)}.git"
            run(["git", "clone", "--quiet", "--bare", "--filter=tree:0", url, str(path)])
            self.clones[url] = path
        return self.clones[url]

    def get(self, url, rev, src_path):
        shipped = src_path / "version"
        if shipped.is_file() and shipped.read_text().strip():
            return shipped.read_text().strip()  # release tags carry their official revision
        repo = self.clone(url)
        getver = src_path / "scripts" / "getver.sh"
        m = re.search(r"REBOOT=([0-9a-f]{40})", getver.read_text()) if getver.exists() else None
        span = f"{m[1]}..{rev}" if m else rev
        count = run(["git", "rev-list", "--count", span], cwd=repo).strip()
        short = run(["git", "rev-parse", "--short=12", rev], cwd=repo).strip()
        return f"r{count}-{short}"


def resolve_channel(name, revisions, published):
    """Return the channel's new lock and notes about held-back feeds."""
    spec = load_json(CHANNELS / name / "channel.json")
    url, ref, rev, version = resolve_src(spec, published)
    src_fetch, src_mtime, src_path = prefetch(fetch_spec(url, rev))
    notes = []
    feeds = []
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
            good = newest_patchable(feed["url"], frev, patches)
            fetch, _, _ = prefetch(fetch_spec(feed["url"], good))
            notes.append(f"feed {feed['name']} pinned to {good[:12]}: patches reject {frev[:12]}")
        entry = {"name": feed["name"], "fetch": fetch, "pinnedBy": pinned_by}
        if feed["sep"] == ";":
            entry["ref"] = feed["ref"]
        if feed["directory"]:
            entry["directory"] = feed["directory"]
        feeds.append(entry)
    lock = {
        "version": version,
        "revision": revisions.get(url, rev, src_path),
        "src": {"ref": ref, "lastModified": src_mtime, "fetch": src_fetch},
        "feeds": feeds,
    }
    return lock, notes


# ---------------------------------------------------------------------------- metadata


def build_metadata(name):
    attr = f'.#"{name}-metadata"'
    proc = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths", attr], cwd=ROOT, text=True, capture_output=True
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-25:])
        raise RefreshError(f"building {attr} failed:\n{tail}")
    return pathlib.Path(proc.stdout.strip())


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

    published = published_releases()
    created = ensure_release_channels(published)
    unknown = sorted(set(args.channels) - set(channel_names()))
    if unknown:
        parser.error(f"unknown channels: {', '.join(unknown)}")

    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        revisions = Revisions(tmp)
        for name in args.channels or channel_names():
            log(f"resolving {name}")
            r = results[name] = {"old": load_json(CHANNELS / name / "lock.json")}
            try:
                r["lock"], r["notes"] = resolve_channel(name, revisions, published)
            except RefreshError as e:
                r["error"] = str(e)
                if name in created:
                    restore(name, None, created)

    # Write candidate locks, then build every resolved channel: this validates new locks as well
    # as unchanged ones against today's flake inputs.
    for name, r in results.items():
        if "error" in r or r["lock"] == r["old"]:
            continue
        write_json(CHANNELS / name / "lock.json", r["lock"])
        run(["git", "add", str(CHANNELS / name)], cwd=ROOT)

    resolved = [name for name, r in results.items() if "error" not in r]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(resolved))) as pool:
        futures = {pool.submit(build_metadata, name): name for name in resolved}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            r = results[name]
            target = CHANNELS / name / "metadata.json"
            try:
                out = future.result()
                if not target.exists() or target.read_bytes() != out.read_bytes():
                    shutil.copyfile(out, target)
                    target.chmod(0o644)
                    run(["git", "add", str(target)], cwd=ROOT)
                    log(f"{name}: metadata updated")
            except RefreshError as e:
                r["error"] = str(e)
                if r["lock"] != r["old"] or name in created:
                    restore(name, r["old"], created)

    rows = ["| channel | status | version | revision | changes |", "|---|---|---|---|---|"]
    for name, r in sorted(results.items()):
        failed = "error" in r
        lock = (r["old"] if failed else r["lock"]) or {}
        change = "" if failed else changes(r["old"], r["lock"])
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
