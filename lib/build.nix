# Building a channel from source, in the official buildbot's configuration (BUILDBOT=y: no
# per-build signing key in the image, reproducible kernel build user and debug paths), with
# three changes: only the requested packages are built (no ALL_KMODS / ALL_NONSHARED), only the
# requested device profiles, and no SDK.
#
#   toolchain target                      tools/ + toolchain/ from the core tree (no feeds), so
#                                         feed updates never rebuild GCC
#   resolve { target profiles packages }  Kconfig on the requested packages, failing if it
#                                         silently drops any (cheap, runs before the builds)
#   imagebuilder { ... }                  kernel + packages on top of the toolchain; the result
#                                         is a standalone ImageBuilder carrying every package
#                                         built, from which images are composed (lib/image.nix)
{
  lib,
  pkgs,
  stdenv,
  hostDeps,
  fhs,
}:
{
  name,
  src,
  tree,
  revision,
  sourceDateEpoch,
  metadata,
  channelSources,
}:
let
  kconfigName = lib.replaceStrings [ "-" "/" ] [ "_" "_" ];

  # target selection and buildbot defaults; shared by every stage so the toolchain stage selects
  # the same tools/ as the package stage (tools/Makefile enables some per configuration)
  baseConfig = target: ''
    CONFIG_TARGET_${kconfigName (lib.head (lib.splitString "/" target))}=y
    CONFIG_TARGET_${kconfigName target}=y
    CONFIG_TARGET_MULTI_PROFILE=y
    # CONFIG_TARGET_ALL_PROFILES is not set
    CONFIG_TARGET_PER_DEVICE_ROOTFS=y
    CONFIG_BUILDBOT=y
    # CONFIG_ALL is not set
    # CONFIG_ALL_NONSHARED is not set
    # CONFIG_ALL_KMODS is not set
    # CONFIG_SDK is not set
    # CONFIG_MAKE_TOOLCHAIN is not set
    # CONFIG_JSON_CYCLONEDX_SBOM is not set
    # CONFIG_COLLECT_KERNEL_DEBUG is not set
    CONFIG_IB=y
    CONFIG_IB_STANDALONE=y
  '';

  # what official builds add on top (config.buildinfo): the version number in image file names
  versionConfig = ''
    CONFIG_IMAGEOPT=y
    CONFIG_VERSIONOPT=y
    CONFIG_VERSION_FILENAMES=y
  '';

  # luci-i18n-<name>-<lang> packages are not selectable: Kconfig enables them for every built
  # LuCI package once LUCI_LANG_<lang> is set
  luciLanguage =
    package:
    let
      match = lib.findFirst (lc: lib.hasSuffix "-${lc}" package) null (
        lib.sort (a: b: lib.stringLength a > lib.stringLength b) (lib.attrNames metadata.luciLanguages)
      );
    in
    if lib.hasPrefix "luci-i18n-" package && match != null then
      metadata.luciLanguages.${match}
    else
      null;

  # Kconfig request for a package list in ImageBuilder syntax ("-pkg" only matters to images).
  # Kconfig selects "+" dependencies but merely requires plain ones (a translation requires what
  # it translates, kmod-sound-soc-* require kmod-sound-core), so those are requested as well.
  packageConfig =
    packages:
    let
      requested = map (p: p.key) (
        builtins.genericClosure {
          startSet = map (key: { inherit key; }) (lib.filter (p: !lib.hasPrefix "-" p) packages);
          operator = { key }: map (key: { inherit key; }) (metadata.packages.${key}.requires or [ ]);
        }
      );
      translations = lib.filter (p: luciLanguage p != null) requested;
    in
    lib.concatMapStrings (l: "CONFIG_LUCI_LANG_${l}=y\n") (lib.unique (map luciLanguage translations))
    + lib.concatMapStrings (p: "CONFIG_PACKAGE_${p}=m\n") (lib.subtractLists translations requested);

  profileConfig =
    target: profiles:
    lib.concatMapStrings (p: "CONFIG_TARGET_DEVICE_${kconfigName target}_DEVICE_${p}=y\n") profiles;

  # Copies `tree` to a writable build directory with `dl` and `config`, then runs defconfig.
  # `owrt_make` runs make inside the FHS view. `make -j` hides why a step failed, so after a
  # failure it prints the logs of failed packages and reruns serially with V=s, as the buildroot
  # suggests; finished steps are skipped, so that output ends with the actual error.
  prelude =
    {
      tree,
      dl ? null,
      config,
    }:
    ''
      unset CC CXX CPP LD AR AS NM RANLIB STRIP OBJCOPY OBJDUMP READELF SIZE STRINGS
      top=$NIX_BUILD_TOP/buildroot
      cp -a --no-preserve=ownership ${tree} $top
      chmod -R u+w $top
      cd $top
      [ -s version ] || echo ${lib.escapeShellArg revision} > version
      [ -s version.date ] || echo ${toString sourceDateEpoch} > version.date
      ${lib.optionalString (dl != null) "mkdir -p dl && cp -P ${dl}/* dl/"}
      cp ${config} .config
      owrt() { owrt-fhs "$NIX_BUILD_TOP" "$@"; }
      owrt_make() {
        owrt make -j''${NIX_BUILD_CORES:-1} BUILD_LOG=1 "$@" && return 0
        { grep -rlE 'Error [0-9]+|error:' logs --include='*.txt' 2>/dev/null || true; } | while read -r f; do
          echo "===== $f"
          tail -n 80 "$f"
        done
        echo "===== rerunning serially"
        owrt make -j1 V=s "$@" 2>&1 | tail -n 300
        return 1
      }
      owrt make defconfig >/dev/null
    '';

  buildrootDrv =
    drvName: attrs:
    stdenv.mkDerivation (
      {
        name = drvName;
        dontUnpack = true;
        dontConfigure = true;
        dontFixup = true;
        hardeningDisable = [ "all" ];
        nativeBuildInputs = hostDeps ++ [ fhs ];
        requiredSystemFeatures = [ "big-parallel" ];
        enableParallelBuilding = true;
        env = {
          # tools/cmake's bundled libarchive looks for iconv.h under FHS prefixes. Only glibc:
          # exposing nixpkgs' openssl here makes cmake's curl mix it with tools/libressl.
          CMAKE_INCLUDE_PATH = "${lib.getDev stdenv.cc.libc}/include";
          # The sandbox is a user namespace where chown(0) fails with EINVAL, which libfakeroot
          # does not ignore.
          FAKEROOTDONTTRYCHOWN = "1";
          # Go modules come from the vendored module cache (lib/vendor.nix), never the network.
          GOPROXY = "off";
        };
      }
      // attrs
    );

  toolchain = lib.genAttrs (lib.attrNames metadata.targets) (
    target:
    buildrootDrv "${name}-${kconfigName target}-toolchain" {
      buildPhase = ''
        ${prelude {
          tree = src;
          dl = channelSources.toolchain "${name}-${kconfigName target}-toolchain" target;
          config = pkgs.writeText "${name}-${kconfigName target}-toolchain.config" (baseConfig target);
        }}
        owrt_make tools/install toolchain/install
      '';
      installPhase = ''
        mkdir -p $out
        cp -a staging_dir $out/
      '';
    }
  );

  # names are checked at evaluation time, before anything is built
  checkRequest =
    {
      target,
      profiles,
      packages,
    }:
    let
      targetInfo =
        metadata.targets.${target} or (throw "immortalwrt.nix: channel ${name} has no target ${target}");
      unknownProfiles = lib.filter (p: !(targetInfo.profiles ? ${p})) profiles;
      names = map (lib.removePrefix "-") packages;
      unknownPackages = lib.filter (p: channelSources.resolveName p == [ ]) names;
      # Kconfig only knows real packages; a virtual name (PROVIDES) has to name its provider
      virtualPackages = lib.filter (
        p: !(metadata.packages ? ${p}) && channelSources.resolveName p != [ ]
      ) names;
    in
    assert lib.assertMsg (profiles != [ ]) "immortalwrt.nix: no device profile requested for ${target}";
    assert lib.assertMsg (unknownProfiles == [ ])
      "immortalwrt.nix: ${target} in channel ${name} has no profile ${lib.concatStringsSep ", " unknownProfiles}";
    assert lib.assertMsg (
      unknownPackages == [ ]
    ) "immortalwrt.nix: channel ${name} has no package ${lib.concatStringsSep ", " unknownPackages}";
    assert lib.assertMsg (virtualPackages == [ ])
      "immortalwrt.nix: request a package that provides ${
        lib.concatMapStringsSep "; " (
          p: "${p} (${lib.concatStringsSep ", " (channelSources.resolveName p)})"
        ) virtualPackages
      }, not the virtual name";
    true;

  # Kconfig resolution of a request: fails if Kconfig dropped a requested package or device, or
  # selected a Go package whose modules are not vendored
  resolve =
    {
      target,
      profiles,
      packages,
    }@request:
    deps:
    stdenv.mkDerivation {
      name = "${name}-${kconfigName target}-config";
      dontUnpack = true;
      dontConfigure = true;
      dontFixup = true;
      hardeningDisable = [ "all" ];
      nativeBuildInputs = hostDeps ++ [ fhs ];
      seed = baseConfig target + versionConfig + profileConfig target profiles + packageConfig packages;
      wanted = lib.concatMapStrings (
        p: "${p} ${if metadata.packages.${p}.hidden or false then "hidden" else "-"}\n"
      ) (lib.filter (p: !lib.hasPrefix "-" p) packages);
      unvendored = lib.concatMapStrings (p: "${p}\n") deps.unvendoredGo;
      passAsFile = [
        "seed"
        "wanted"
        "unvendored"
      ];
      buildPhase = ''
        ${prelude {
          inherit tree;
          config = "$seedPath";
        }}
        enabled() { grep -qxF "CONFIG_PACKAGE_$1=y" .config || grep -qxF "CONFIG_PACKAGE_$1=m" .config; }
        dropped=""
        while read -r p hidden; do
          if ! enabled "$p"; then
            dropped="$dropped $p"
            [ "$hidden" = - ] || dropped="$dropped (only built as a dependency of another package)"
          fi
        done < $wantedPath
        if [ -n "$dropped" ]; then
          echo "Kconfig dropped requested packages (conflicting or not available for ${target}):$dropped" >&2
          exit 1
        fi
        for p in ${lib.escapeShellArgs profiles}; do
          grep -qxF "CONFIG_TARGET_DEVICE_${kconfigName target}_DEVICE_$p=y" .config \
            || { echo "Kconfig dropped device profile $p" >&2; exit 1; }
        done
        unvendored=""
        while read -r p; do
          [ -z "$p" ] || ! enabled "$p" || unvendored="$unvendored $p"
        done < $unvendoredPath
        if [ -n "$unvendored" ]; then
          echo "Go packages without vendored modules in channel ${name}:$unvendored" >&2
          echo "Add them to vendor-packages.json and run the refresh." >&2
          exit 1
        fi
      '';
      installPhase = "cp .config $out";
    };

  imagebuilder =
    {
      target,
      profiles,
      packages,
    }@request:
    let
      drvName = "${name}-${kconfigName target}-imagebuilder";
      deps =
        assert checkRequest request;
        channelSources.packages drvName request;
      config = resolve request deps;
      parts = lib.splitString "/" target;
    in
    buildrootDrv drvName {
      buildPhase = ''
        ${prelude {
          inherit tree config;
          inherit (deps) dl;
        }}
        ${lib.optionalString (deps.goModules != [ ]) ''
          mkdir -p dl/go-mod-cache/cache/download
          for m in ${lib.escapeShellArgs deps.goModules}; do
            cp -r --no-preserve=mode $m/. dl/go-mod-cache/cache/download/
          done
        ''}
        # defconfig's prerequisite checks already created staging_dir/host; replace it, or `cp`
        # would copy the toolchain into it
        rm -rf staging_dir
        cp -r ${toolchain.${target}}/staging_dir staging_dir
        chmod -R u+w staging_dir
        # `world` decides from this stamp whether to rebuild the toolchain (the build directories
        # that would let it check component by component are not kept); it has to be the newest
        for d in staging_dir/toolchain-*/stamp; do touch "$d/.toolchain_compile"; done
        # With CONFIG_IB the buildroot compiles and installs the kernel of every device of the
        # target, so the ImageBuilder can serve any of them. Official builds select all profiles;
        # with a subset, devices whose kernel embeds their own root filesystem (netgear_eax17's
        # FIT carries a rootfs hash) fail. This ImageBuilder only serves the requested profiles
        # (lib/image.nix checks), so build only their kernels.
        substituteInPlace include/image.mk \
          --replace-fail \
            '_COMPILE_TARGET := $$(if $(CONFIG_IB)$$(_PROFILE_SET),compile,compile-disabled)' \
            '_COMPILE_TARGET := $$(if $$(_PROFILE_SET),compile,compile-disabled)' \
          --replace-fail \
            '      install: $$(KDIR_KERNEL_IMAGE)' \
            '      $$(_TARGET): $$(KDIR_KERNEL_IMAGE)'
        owrt_make world
      '';
      installPhase = ''
        mkdir -p $out
        tar -C $out --strip-components=1 -xf bin/targets/${lib.head parts}/${lib.last parts}/*-imagebuilder-*.tar.zst
      '';
      passthru = {
        inherit
          target
          profiles
          packages
          config
          ;
        channel = name;
      };
    };
in
{
  inherit toolchain imagebuilder;
}
