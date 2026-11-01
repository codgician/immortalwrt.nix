# A channel: one upstream source (a branch, the newest tag of a release series, or a fork),
# pinned together with every feed by channels/<name>/lock.json.
{
  lib,
  fetchLocked,
  mkTree,
  mkMetadata,
  mkImage,
  binaryReleases,
}:
{
  name,
  spec,
  lock,
  metadataFile ? null,
}:
let
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
  };
  self = {
    inherit
      name
      spec
      lock
      src
      feeds
      tree
      ;
    inherit (lock) version revision;
    # committed copy of `metadataDrv`, readable at evaluation time; null until the first refresh
    metadata =
      if metadataFile != null && builtins.pathExists metadataFile then
        lib.importJSON metadataFile
      else
        null;
    metadataDrv = mkMetadata { inherit name tree; };
  }
  # Images need the official ImageBuilder of the locked release; the refresh only advances
  # release channels to tags whose binaries are pinned (see binaryReleases).
  // lib.optionalAttrs (builtins.elem lock.version binaryReleases) {
    mkImage = mkImage self;
  };
in
self
