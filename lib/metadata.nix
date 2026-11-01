# Package and target metadata of a tree, as produced by the buildroot's own scan
# (`make prepare-tmpinfo`). The refresh job commits the result as channels/<name>/metadata.json
# so consumers can check package and profile names at evaluation time without IFD.
{
  stdenv,
  hostDeps,
  fhs,
}:
{ name, tree }:
stdenv.mkDerivation {
  name = "${name}-metadata.json";
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
    runHook postBuild
  '';

  installPhase = "cp metadata.json $out";
}
