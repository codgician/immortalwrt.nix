# Every directory here with a channel.json and a lock.json is a channel. Release-series channels
# (immortalwrt-<series>, immortalwrt-<series>-snapshot) are created by the refresh job when
# upstream opens a new series; `latest-release` / `latest-release-snapshot` follow the newest one.
{ lib, mkChannel }:
let
  dir = ./.;
  isChannel =
    name: type:
    type == "directory"
    && builtins.pathExists (dir + "/${name}/channel.json")
    && builtins.pathExists (dir + "/${name}/lock.json");
  channels = lib.mapAttrs (
    name: _:
    mkChannel {
      inherit name;
      spec = lib.importJSON (dir + "/${name}/channel.json");
      lock = lib.importJSON (dir + "/${name}/lock.json");
      metadataFile = dir + "/${name}/metadata.json";
      sourcesFile = dir + "/${name}/sources.json";
      vendorFile = dir + "/${name}/vendor.json";
    }
  ) (lib.filterAttrs isChannel (builtins.readDir dir));

  newest =
    pred: key:
    let
      candidates = lib.filter pred (lib.attrValues channels);
    in
    lib.foldl' (
      best: c: if best == null || lib.versionOlder (key best) (key c) then c else best
    ) null candidates;
  latestRelease = newest (c: c.spec.follow ? tagSeries) (c: c.version);
  latestReleaseSnapshot = newest (c: c.spec.follow ? branch && c.spec ? series) (c: c.spec.series);
in
{
  inherit channels;
  aliases =
    lib.optionalAttrs (latestRelease != null) { latest-release = latestRelease; }
    // lib.optionalAttrs (latestReleaseSnapshot != null) {
      latest-release-snapshot = latestReleaseSnapshot;
    };
}
