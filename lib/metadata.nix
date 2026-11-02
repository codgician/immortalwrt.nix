# What the buildroot itself says about a tree, as committed by the refresh job:
#
# - metadata.json: packages, their dependencies and defaults, targets and device profiles, from
#   the buildroot's own scan (`make prepare-tmpinfo`). Consumers check names against it at
#   evaluation time, and source closures are computed from it, without IFD.
# - sources.json: every file the tree can download, keyed by the directory that needs it, from
#   the buildroot's own `make -n download` (scripts/sources.py).
{
  stdenv,
  hostDeps,
  fhs,
}:
{ name, tree }:
stdenv.mkDerivation {
  name = "${name}-metadata";
  dontUnpack = true;
  dontConfigure = true;
  dontFixup = true;
  hardeningDisable = [ "all" ];
  nativeBuildInputs = hostDeps ++ [ fhs ];

  buildPhase = ''
    runHook preBuild
    cp -r ${tree} "$NIX_BUILD_TOP/tree"
    chmod -R u+w "$NIX_BUILD_TOP/tree"
    cd "$NIX_BUILD_TOP/tree"
    owrt-fhs "$NIX_BUILD_TOP" make prepare-tmpinfo
    python3 ${../scripts/metadata.py} tmp/.packageinfo tmp/.targetinfo "$(cat version)" > metadata.json
    python3 ${../scripts/sources.py} "$(cat version)" > sources.json
    runHook postBuild
  '';

  installPhase = ''
    mkdir $out
    cp metadata.json sources.json $out/
  '';
}
