# A channel: one upstream source (a branch, the newest tag of a release series, or a fork),
# pinned together with every feed by channels/<name>/lock.json, and built from source.
{
  lib,
  fetchLocked,
  mkTree,
  mkMetadata,
  mkSources,
  mkBuild,
  mkImage,
}:
{
  name,
  spec,
  lock,
  # committed outputs of the refresh job (null until the first refresh)
  metadataFile ? null,
  sourcesFile ? null,
  vendorFile ? null,
}:
let
  importIfExists =
    file: default: if file != null && builtins.pathExists file then lib.importJSON file else default;

  src = fetchLocked lock.src.fetch;
  feeds = map (feed: {
    inherit (feed) name;
    directory = feed.directory or null;
    src = fetchLocked feed.fetch;
  }) lock.feeds;
  tree = mkTree {
    inherit name src feeds;
    inherit (lock) revision;
    sourceDateEpoch = lock.src.lastModified;
    feedPatches = spec.feedPatches or null;
    packageVersions = lock.packageVersions or null;
  };

  metadata = importIfExists metadataFile null;
  sources = importIfExists sourcesFile null;
  vendor = importIfExists vendorFile { };

  channelSources = mkSources {
    inherit
      src
      tree
      metadata
      sources
      vendor
      ;
  };
  build = mkBuild {
    inherit
      name
      src
      tree
      metadata
      channelSources
      ;
    inherit (lock) revision;
    sourceDateEpoch = lock.src.lastModified;
  };

  # A standalone ImageBuilder for `target`, built from source with `packages` (ImageBuilder
  # syntax) and the device `profiles`; `.mkImage` composes images from it.
  imagebuilder =
    request:
    let
      ib = build.imagebuilder request;
    in
    ib // { mkImage = mkImage ib; };

  # Images for a set of devices: { <name> = { target; profile; packages; files; ... }; }.
  # Devices on the same target share one ImageBuilder, built with the union of their packages
  # and profiles, so their kernels and packages are identical.
  mkImages =
    devices:
    let
      byTarget = lib.groupBy (n: devices.${n}.target) (lib.attrNames devices);
      imagebuilders = lib.mapAttrs (
        target: names:
        imagebuilder {
          inherit target;
          profiles = lib.unique (map (n: devices.${n}.profile) names);
          packages = lib.unique (
            lib.filter (p: !lib.hasPrefix "-" p) (lib.concatMap (n: devices.${n}.packages or [ ]) names)
          );
        }
      ) byTarget;
    in
    lib.mapAttrs (
      n: device: imagebuilders.${device.target}.mkImage ({ name = n; } // removeAttrs device [ "target" ])
    ) devices;

  ready =
    metadata != null
    && sources != null
    && metadata.revision == lock.revision
    && sources.revision == lock.revision;
  needRefresh = throw "immortalwrt.nix: channel ${name} has no metadata for its locked revision; run `nix run .#refresh`";
in
{
  inherit
    name
    spec
    lock
    src
    feeds
    tree
    metadata
    sources
    vendor
    ;
  inherit (lock) version revision;
  # derivation producing metadata.json and sources.json (see lib/metadata.nix)
  metadataDrv = mkMetadata { inherit name tree; };
  # vendored Go modules by package directory (see vendor-packages.json and lib/vendor.nix)
  goModules = lib.genAttrs (lib.attrNames vendor) channelSources.goModules;
  # the same with an empty hash, for every Go package directory: the refresh builds these to
  # learn the hashes it records in vendor.json
  goModulesProbe = lib.genAttrs (lib.attrNames (
    lib.filterAttrs (_: s: s.go or false) metadata.sources
  )) (dir: channelSources.goModulesWithHash dir "");
  # Results throw when the channel's metadata does not match its lock, but attribute names stay
  # available, so flakes can list outputs while the refresh rewrites the lock.
  # stage outputs, for building ahead of time and caching: toolchain.<target>
  toolchain = if ready then build.toolchain else needRefresh;
  imagebuilder = request: if ready then imagebuilder request else needRefresh;
  mkImages = devices: if ready then mkImages devices else lib.mapAttrs (_: _: needRefresh) devices;
  # a single device, with its own ImageBuilder; see mkImages
  mkImage =
    {
      target,
      profile,
      packages ? [ ],
      ...
    }@device:
    if ready then
      (imagebuilder {
        inherit target;
        profiles = [ profile ];
        packages = lib.filter (p: !lib.hasPrefix "-" p) packages;
      }).mkImage
        (removeAttrs device [ "target" ])
    else
      needRefresh;
}
