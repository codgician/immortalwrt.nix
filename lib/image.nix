# Firmware images composed by a channel's own ImageBuilder (lib/build.nix) from a device
# profile, a package list in ImageBuilder syntax ("-pkg" drops a default package) and files
# copied over the root filesystem. Composition is cheap: per-device changes never rebuild
# packages, only the ImageBuilder's package set does.
#
# The ImageBuilder's .config can be adjusted in `postConfigure` (the working directory is the
# ImageBuilder), e.g. to skip image formats.
{
  lib,
  stdenv,
  hostDeps,
  fhs,
}:
imagebuilder:
{
  profile,
  packages ? [ ],
  # directory (or derivation) copied over the root filesystem
  files ? null,
  # appended to image file names, e.g. the device's host name
  name ? null,
  rootFsPartSize ? null,
  disabledServices ? [ ],
}:
assert lib.assertMsg (lib.elem profile imagebuilder.profiles)
  "immortalwrt.nix: the ${imagebuilder.target} ImageBuilder of channel ${imagebuilder.channel} was not built for profile ${profile} (built: ${lib.concatStringsSep ", " imagebuilder.profiles})";
stdenv.mkDerivation {
  name = lib.concatStringsSep "-" (
    [
      imagebuilder.channel
      profile
    ]
    ++ lib.optional (name != null) name
  );
  dontUnpack = true;
  dontFixup = true;
  hardeningDisable = [ "all" ];
  nativeBuildInputs = hostDeps ++ [ fhs ];
  env.FAKEROOTDONTTRYCHOWN = "1";

  configurePhase = ''
    runHook preConfigure
    cp -a --no-preserve=ownership ${imagebuilder} $NIX_BUILD_TOP/imagebuilder
    chmod -R u+w $NIX_BUILD_TOP/imagebuilder
    cd $NIX_BUILD_TOP/imagebuilder
    # The package index is generated here and never leaves the build, so there is nothing to
    # sign; signing would also generate a random key each time.
    sed -i 's/^CONFIG_SIGNATURE_CHECK=y$/# CONFIG_SIGNATURE_CHECK is not set/' .config
    runHook postConfigure
  '';

  buildPhase = ''
    runHook preBuild
    owrt-fhs "$NIX_BUILD_TOP" make image \
      PROFILE=${lib.escapeShellArg profile} \
      PACKAGES=${lib.escapeShellArg (toString packages)} \
      BIN_DIR="$NIX_BUILD_TOP/images" \
      ${lib.optionalString (files != null) "FILES=${files}"} \
      ${lib.optionalString (name != null) "EXTRA_IMAGE_NAME=${lib.escapeShellArg name}"} \
      ${lib.optionalString (rootFsPartSize != null) "ROOTFS_PARTSIZE=${toString rootFsPartSize}"} \
      ${lib.optionalString (
        disabledServices != [ ]
      ) "DISABLED_SERVICES=${lib.escapeShellArg (toString disabledServices)}"}
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    cp -r "$NIX_BUILD_TOP/images" $out
    runHook postInstall
  '';

  passthru = {
    inherit imagebuilder profile packages;
  };
}
