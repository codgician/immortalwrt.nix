{
  pkgs,
  lib,
  imagebuilder,
}:
let
  # The buildroot is validated against this compiler generation (OpenWrt's own toolchain is
  # GCC 14); the newest nixpkgs GCC tends to break older host tools in tools/.
  stdenv = pkgs.gcc14Stdenv;
  hostDeps = import ./host-deps.nix { inherit pkgs; };
  fhs = import ./fhs.nix { inherit pkgs; };
  fetchLocked = import ./fetch.nix { inherit pkgs; };
  mkTree = import ./tree.nix {
    inherit
      lib
      stdenv
      hostDeps
      fhs
      ;
  };
  mkMetadata = import ./metadata.nix { inherit stdenv hostDeps fhs; };
  mkImage = import ./image.nix { inherit lib pkgs imagebuilder; };
  # releases whose official ImageBuilder and package hashes are pinned by nix-immortalwrt-imagebuilder
  binaryReleases = lib.filter (r: r != "snapshot") imagebuilder.lib.releases;
  mkChannel = import ./channel.nix {
    inherit
      lib
      fetchLocked
      mkTree
      mkMetadata
      mkImage
      binaryReleases
      ;
  };
in
{
  inherit
    stdenv
    hostDeps
    fhs
    fetchLocked
    mkTree
    mkMetadata
    mkImage
    binaryReleases
    mkChannel
    ;
}
