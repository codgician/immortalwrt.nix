# A buildroot source tree with its feeds installed, ready to configure and build.
#
# Feeds come from the channel lock as store paths and are installed with `src-cpy`, so nothing
# here touches the network. Fork-style feed patches (`<feedPatches>/<feed>/**.patch`, applied with
# `git apply` relative to the feed root) are applied in byte order of their paths; a patch whose
# change the feed already contains is skipped, and so are patches for feeds the tree does not use,
# matching naoki66's scripts/apply-feed-patches.sh.
# The result is read-only: later stages copy it, so build artifacts can never leak into the
# source tree (which, for example, would make luci.mk's mtime-based versions time-dependent).
{
  lib,
  stdenv,
  hostDeps,
  fhs,
}:
{
  name,
  src,
  # [ { name; src; directory; } ] in feeds.conf order; directory is null unless --directory is used
  feeds,
  # used only when the tree does not ship its own `version` / `version.date` (release tags do)
  revision,
  sourceDateEpoch,
  # path inside the tree holding per-feed patches, e.g. "patches/feeds"
  feedPatches ? null,
}:
stdenv.mkDerivation {
  name = "${name}-tree";
  dontUnpack = true;
  dontConfigure = true;
  dontFixup = true;
  hardeningDisable = [ "all" ];
  nativeBuildInputs = hostDeps ++ [ fhs ];

  feedsConf = lib.concatMapStrings (
    feed:
    "src-cpy ${
      lib.optionalString (feed.directory != null) "--directory=${feed.directory} "
    }${feed.name} ${feed.src}\n"
  ) feeds;
  passAsFile = [ "feedsConf" ];

  buildPhase = ''
    runHook preBuild

    tree=$NIX_BUILD_TOP/tree
    cp -r ${src} "$tree"
    chmod -R u+w "$tree"
    cd "$tree"
    owrt() { owrt-fhs "$NIX_BUILD_TOP" "$@"; }

    cp "$feedsConfPath" feeds.conf
    owrt ./scripts/feeds update -a
    chmod -R u+w feeds
    owrt ./scripts/feeds install -a

    ${lib.optionalString (feedPatches != null) ''
      while IFS= read -r -d "" patch; do
        rel=''${patch#${feedPatches}/}
        feed=''${rel%%/*}
        if [ ! -d "feeds/$feed" ]; then
          echo "Feed patch skipped, feed $feed is not used: $patch"
        elif git -C "feeds/$feed" apply --check < "$patch" 2>/dev/null; then
          echo "Applying feed patch: $patch"
          git -C "feeds/$feed" apply < "$patch"
        elif git -C "feeds/$feed" apply --reverse --check < "$patch" 2>/dev/null; then
          echo "Feed patch already present: $patch"
        else
          echo "Feed patch does not apply: $patch" >&2
          exit 1
        fi
      done < <(find ${feedPatches} -type f -name '*.patch' -print0 | LC_ALL=C sort -z)
    ''}

    [ -s version ] || echo ${lib.escapeShellArg revision} > version
    [ -s version.date ] || echo ${toString sourceDateEpoch} > version.date
    rm -rf tmp

    runHook postBuild
  '';

  installPhase = ''
    cp -a . $out
  '';
}
