# immortalwrt.nix

ImmortalWrt source channels, pinned and built from source with Nix.

Official ImageBuilders only cover devices that upstream ships. This project pins the actual source trees instead and builds them, so one Nix interface covers both upstream ImmortalWrt and forks that add their own targets, kernel patches and feeds. The OpenWrt buildroot remains the compiler; Nix pins every input, fetches every source file as a fixed-output derivation, runs the build in its sandbox and caches each stage. See [docs/design.md](docs/design.md).

## Channels

| Channel | Follows |
|---|---|
| `immortalwrt-snapshot` | `master` of [immortalwrt/immortalwrt](https://github.com/immortalwrt/immortalwrt) |
| `immortalwrt-<series>-snapshot` | the newest release branch, e.g. `openwrt-25.12` |
| `immortalwrt-<series>` | the newest stable tag of the newest release series, e.g. `v25.12.2` |
| `gemtek` | `master` of [naoki66/ImmortalWrt-for-Gemtek-brightspeed](https://github.com/naoki66/ImmortalWrt-for-Gemtek-brightspeed) (Gemtek XR1710G / XG2010G) |

The aliases `latest-release` and `latest-release-snapshot` point at the newest release-series channels. When upstream opens a new release branch or tags a new series, the refresh job creates the matching channels on its own.

Every channel lives in `channels/<name>/`:

- `channel.json` says what to follow.
- `lock.json` pins the upstream commit and every feed. Each feed pin records where it came from: the tree itself (release tags carry `^commit` pins), a feed branch, or the feed's default branch.
- `metadata.json` lists the targets, device profiles and packages of that exact tree, with their dependencies. Names are checked against it at evaluation time.
- `sources.json` lists every source file the tree can download, with the hashes the buildroot itself declares.
- `vendor.json` holds the hashes of the Go modules of the packages listed in [`vendor-packages.json`](vendor-packages.json).

## Usage

```nix
{
  inputs.immortalwrt.url = "github:codgician/immortalwrt.nix";

  outputs = { immortalwrt, ... }:
    let
      channels = immortalwrt.legacyPackages.x86_64-linux.channels;
    in
    {
      packages.x86_64-linux = channels."immortalwrt-25.12".mkImages {
        # attribute names are appended to image file names unless `name` says otherwise
        my-router = {
          target = "mediatek/filogic";
          profile = "glinet_gl-xe3000";
          packages = [ "luci-ssl" "-libustream-openssl" "luci-i18n-base-zh-cn" ];
          files = ./files; # copied over the root filesystem
        };
        my-other-router = {
          target = "mediatek/filogic";
          profile = "bananapi_bpi-r3-mini";
          packages = [ "luci" "samba4-server" ];
          rootFsPartSize = 7384;
        };
      };
    };
}
```

A build has three stages, each its own derivation:

1. **Toolchain** (`channels.<name>.toolchain.<target>`): host tools and the cross toolchain, from the core tree without feeds.
2. **ImageBuilder** (`channels.<name>.imagebuilder { target; profiles; packages; }`): the kernel and every requested package, built in the official buildbot's configuration, packed into a standalone ImageBuilder.
3. **Images** (`<imagebuilder>.mkImage { profile; packages; files; ... }`): composed by that ImageBuilder, which takes seconds.

`mkImages` groups devices by target, so devices on the same target share one ImageBuilder built with the union of their packages and profiles. `mkImage` builds a single device with its own ImageBuilder. Both also accept `rootFsPartSize` and `disabledServices`, and return regular derivations, so `overrideAttrs` works as usual; the ImageBuilder's `.config` can be adjusted in `postConfigure`.

Package lists use ImageBuilder syntax, and names are checked against the channel when the flake is evaluated. Before anything expensive is built, Kconfig resolves the request, and the build fails if it dropped a requested package (one not available on the target, or one that can only be built as a dependency of another package). Unlike official builds, only the requested packages and kernel modules are built, so official kmod packages do not match these kernels.

Go packages download their modules while building, which the sandbox does not allow. Their modules are vendored per channel for the packages in [`vendor-packages.json`](vendor-packages.json); a request that selects any other Go package fails with a message saying so.

The `gemtek-xr1710g` and `gemtek-xr1710g-ubi` packages of this flake build a device that exists only in a fork:

```sh
nix build github:codgician/immortalwrt.nix#gemtek-xr1710g
nix build github:codgician/immortalwrt.nix#gemtek-tree    # any channel's configured source tree
```

## Binary cache

Stages built by CI are pushed to `https://cache.codgician.me/immortalwrt`. The flake's `nixConfig` offers it; accept it, or add it yourself:

```nix
nix.settings = {
  extra-substituters = [ "https://cache.codgician.me/immortalwrt" ];
  extra-trusted-public-keys = [ "immortalwrt:t17gIHmdp6G9DpukeaW0LCB32Lg+mPRvN0SNdGW1r90=" ];
};
```

## Refreshing

```sh
nix run .#refresh                       # all channels
nix run .#refresh -- gemtek             # one channel
```

For each channel, the refresh:

1. resolves the upstream commit;
2. pins each feed from the tree's `feeds.conf.default`, and records the package versions a git checkout would give (base-files' release, LuCI package versions);
3. prefetches all sources;
4. rebuilds the tree, its metadata and its sources lock;
5. hashes the Go modules of the vendored packages whose source or patches changed.

A new lock is only kept when all of that succeeds. If anything fails, the channel stays on its last good state. Forks that patch their feeds (`feedPatches` in `channel.json`) only move each feed to the newest commit their patches still apply to.

[`.github/workflows/refresh.yml`](.github/workflows/refresh.yml) runs this daily and commits the result. `nixpkgs` is updated by hand: every update changes the host tools of every stage, and so rebuilds them all.

## Requirements

- An `x86_64-linux` builder with the Nix sandbox enabled and the `big-parallel` system feature.
- Nested unprivileged user namespaces inside the sandbox, because the buildroot runs in a small bubblewrap FHS view. On Ubuntu 24.04 this needs `sysctl kernel.apparmor_restrict_unprivileged_userns=0`.
- Time and disk: a toolchain and an ImageBuilder each take hours on a 4-core machine and tens of gigabytes while building, which is why CI builds them in separate jobs and caches them.

## License

GPL-2.0-only, the same as the OpenWrt buildroot this project builds on.
