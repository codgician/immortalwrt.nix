# A buildroot source tree with its feeds installed, ready to configure and build.
#
# Feeds come from the channel lock as store paths and are installed with `src-cpy`, so nothing
# here touches the network. Fork-style feed patches (`<feedPatches>/<feed>/**.patch`, applied with
# `git apply` relative to the feed root) are applied in byte order of their paths; a patch whose
# change the feed already contains is skipped, and so are patches for feeds the tree does not use,
# matching naoki66's scripts/apply-feed-patches.sh.
# Without `.git` the buildroot derives some package versions from build-time values; the lock's
# `packageVersions` (computed by the refresh from git history) restores base-files' release and the
# LuCI package versions a git checkout would give.
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
  # { baseFilesRelease; luci = { <package dir> = { src; po; }; }; } from the lock, if recorded
  packageVersions ? null,
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

    # luci.mk versions packages by the newest mtime in the package directory when there is no
    # .git. luci-base compiles its host tools inside that directory, which would make its version
    # depend on the build time; build them in the host build directory instead.
    if [ -f feeds/luci/modules/luci-base/Makefile ]; then
      substituteInPlace feeds/luci/modules/luci-base/Makefile \
        --replace-fail '$(MAKE) -C src/ clean po2lmo jsmin' \
          'rm -rf $(HOST_BUILD_DIR)/src && mkdir -p $(HOST_BUILD_DIR) && $(CP) src $(HOST_BUILD_DIR)/src && $(MAKE) -C $(HOST_BUILD_DIR)/src clean po2lmo jsmin' \
        --replace-fail '$(INSTALL_BIN) src/po2lmo' '$(INSTALL_BIN) $(HOST_BUILD_DIR)/src/po2lmo' \
        --replace-fail '$(INSTALL_BIN) src/jsmin' '$(INSTALL_BIN) $(HOST_BUILD_DIR)/src/jsmin'
    fi

    ${lib.optionalString (packageVersions != null) ''
      substituteInPlace package/base-files/Makefile \
        --replace-fail 'PKG_RELEASE:=$(COMMITCOUNT)' ${lib.escapeShellArg "PKG_RELEASE:=${packageVersions.baseFilesRelease}"}
      ${lib.concatStrings (
        lib.mapAttrsToList (dir: v: ''
          sed -i -e ${lib.escapeShellArg "1i PKG_SRC_VERSION:=${v.src}"} -e ${lib.escapeShellArg "1i PKG_PO_VERSION:=${v.po}"} ${lib.escapeShellArg "${dir}/Makefile"}
        '') packageVersions.luci
      )}
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
