# immortalwrt.nix

ImmortalWrt source channels, pinned and built with Nix.

Official ImageBuilders only cover devices that upstream ships. This project pins the actual source trees instead, so one Nix interface covers both upstream ImmortalWrt and forks that add their own targets, kernel patches and feeds.

> **Status**
> - Every channel pins a source tree together with all of its feeds.
> - CI refreshes the pins daily and builds each tree and its package metadata in the Nix sandbox.
> - Release channels can already build firmware with the official ImageBuilder.
> - Building firmware from source (needed for fork-only devices) is next; see [docs/design.md](docs/design.md).

## Channels

| Channel | Follows | Images |
|---|---|---|
| `immortalwrt-snapshot` | `master` of [immortalwrt/immortalwrt](https://github.com/immortalwrt/immortalwrt) | — |
| `immortalwrt-<series>-snapshot` | the newest release branch, e.g. `openwrt-25.12` | — |
| `immortalwrt-<series>` | the newest published stable tag of the newest release series, e.g. `v25.12.2` | `mkImage` |
| `gemtek` | `master` of [naoki66/ImmortalWrt-for-Gemtek-brightspeed](https://github.com/naoki66/ImmortalWrt-for-Gemtek-brightspeed) (Gemtek XR1710G / XG2010G) | — |

The aliases `latest-release` and `latest-release-snapshot` point at the newest release-series channels. When upstream opens a new release branch or publishes a new series, the refresh job creates the matching channels on its own.

Every channel lives in `channels/<name>/`:

- `channel.json` says what to follow.
- `lock.json` pins the upstream commit and every feed. Each feed pin records where it came from: the tree itself (release tags carry `^commit` pins), a feed branch, or the feed's default branch.
- `metadata.json` lists the targets, device profiles and packages of that exact tree. They are checked at evaluation time.

## Usage

```nix
{
  inputs.immortalwrt.url = "github:codgician/immortalwrt.nix";

  outputs = { immortalwrt, ... }:
    let
      channels = immortalwrt.legacyPackages.x86_64-linux.channels;
    in
    {
      packages.x86_64-linux = {
        # firmware from a release channel; target and profile are checked against channel metadata
        my-router = channels."immortalwrt-25.12".mkImage {
          name = "my-router"; # appended to image file names
          target = "mediatek/filogic";
          profile = "glinet_gl-xe3000";
          packages = [ "luci-ssl" "-libustream-openssl" ];
          files = ./files; # copied over the root filesystem
        };
        # the configured source tree of any channel (feeds installed, fork feed patches applied)
        gemtek-tree = channels.gemtek.tree;
      };
    };
}
```

`mkImage` also accepts `rootFsPartSize` and `disabledServices`. It returns a regular derivation, so `overrideAttrs` works as usual.

```sh
nix build github:codgician/immortalwrt.nix#gemtek-tree
nix build 'github:codgician/immortalwrt.nix#"immortalwrt-25.12-metadata"'
```

## Refreshing

```sh
nix run .#refresh                       # all channels
nix run .#refresh -- gemtek             # one channel
```

For each channel, the refresh:

1. resolves the upstream commit;
2. pins each feed from the tree's `feeds.conf.default`;
3. prefetches all sources;
4. rebuilds the tree and its metadata.

A new lock is only kept when that build succeeds. If it fails, the channel stays on its last good lock.

Two kinds of channel advance more cautiously:

- **Release channels** only move to a tag once [nix-immortalwrt-imagebuilder](https://github.com/codgician/nix-immortalwrt-imagebuilder) has pinned its published binaries. This guarantees that `mkImage` always works.
- **Forks that patch their feeds** (`feedPatches` in `channel.json`) only move each feed to the newest commit their patches still apply to.

[`.github/workflows/refresh.yml`](.github/workflows/refresh.yml) runs this daily, together with `nix flake update`, and commits the result.

## Requirements

- An `x86_64-linux` builder with the Nix sandbox enabled.
- Nested unprivileged user namespaces inside the sandbox, because the buildroot runs in a small bubblewrap FHS view. On Ubuntu 24.04 this needs `sysctl kernel.apparmor_restrict_unprivileged_userns=0`.

## License

GPL-2.0-only, the same as the OpenWrt buildroot this project builds on.
