# Firmware images for a channel, composed by an ImageBuilder from a device profile, a package
# list and extra files. Package lists use ImageBuilder semantics ("-pkg" drops a default package).
#
# Release channels use the official ImageBuilder and package repositories through
# nix-immortalwrt-imagebuilder, which pins their hashes daily. Target and profile are checked
# against the channel metadata; package names are left to the ImageBuilder, because the official
# package repositories are rebuilt from the release branch and carry packages the tag does not.
# Building the ImageBuilder from the channel's own tree instead is the next step, see
# docs/design.md.
{
  lib,
  pkgs,
  imagebuilder,
}:
channel:
{
  # "<target>/<subtarget>", e.g. "mediatek/filogic"
  target,
  # device profile within that target, e.g. "glinet_gl-xe3000"
  profile,
  packages ? [ ],
  # directory (or derivation) copied over the root filesystem
  files ? null,
  # appended to image file names, e.g. the router's host name
  name ? null,
  rootFsPartSize ? null,
  disabledServices ? [ ],
}:
let
  targetInfo =
    channel.metadata.targets.${target}
      or (throw "immortalwrt.nix: channel ${channel.name} has no target ${target}");
  parts = lib.splitString "/" target;
in
assert lib.assertMsg (builtins.elem profile targetInfo.profiles)
  "immortalwrt.nix: ${target} in channel ${channel.name} has no profile ${profile}";
imagebuilder.lib.build {
  inherit
    pkgs
    profile
    packages
    files
    rootFsPartSize
    disabledServices
    ;
  release = channel.version;
  target = lib.head parts;
  variant = lib.last parts;
  extraImageName = name;
}
