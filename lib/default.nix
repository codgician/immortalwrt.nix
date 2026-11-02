{
  pkgs,
  lib,
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
  mkGoModules = import ./vendor.nix { inherit lib pkgs; };
  mkSources = import ./sources.nix { inherit lib pkgs mkGoModules; };
  mkBuild = import ./build.nix {
    inherit
      lib
      pkgs
      stdenv
      hostDeps
      fhs
      ;
  };
  mkImage = import ./image.nix {
    inherit
      lib
      stdenv
      hostDeps
      fhs
      ;
  };
  mkChannel = import ./channel.nix {
    inherit
      lib
      fetchLocked
      mkTree
      mkMetadata
      mkSources
      mkBuild
      mkImage
      ;
  };
in
{
  inherit mkChannel;
}
