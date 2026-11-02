# Go modules for a Go package of a channel, as a fixed-output derivation.
#
# golang-package.mk builds with GOMODCACHE=$(DL_DIR)/go-mod-cache and lets `go` download modules
# during the build, which the sandbox forbids. This downloads them ahead of time, like nixpkgs'
# `proxyVendor`: the output is the module cache's download directory, which `go` reads offline
# (GOPROXY=off) once copied to dl/go-mod-cache/cache/download.
#
# The hash is maintained by the refresh job in channels/<name>/vendor.json, keyed by the source
# directory and recomputed whenever the package's source file changes.
{
  lib,
  pkgs,
}:
{
  # store name prefix, e.g. the source file name
  name,
  # the package's source archive (a dl/ file from the sources lock)
  source,
  # the package's patches directory in the channel tree (may not exist), as go.mod can be patched
  patches,
  hash,
}:
pkgs.stdenvNoCC.mkDerivation {
  name = "${lib.strings.sanitizeDerivationName name}-go-modules";
  nativeBuildInputs = [
    pkgs.go
    pkgs.cacert
    pkgs.gnutar
    pkgs.zstd
    pkgs.xz
    pkgs.unzip
  ];
  outputHashMode = "recursive";
  outputHashAlgo = "sha256";
  outputHash = hash;
  impureEnvVars = lib.fetchers.proxyImpureEnvVars ++ [ "GOPROXY" ];
  env = {
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    GOTOOLCHAIN = "local";
    GOFLAGS = "-modcacherw";
  };
  buildCommand = ''
    export HOME=$TMPDIR GOPATH=$TMPDIR/go GOMODCACHE=$TMPDIR/modcache
    mkdir src && cd src
    case ${source.name} in
      *.zip) unzip -q ${source} ;;
      *) tar xf ${source} ;;
    esac
    # the package build unpacks with --strip-components=1
    [ "$(ls -A | wc -l)" = 1 ] && cd *
    if [ -d ${patches} ]; then
      for p in $(find ${patches} -maxdepth 1 -name '*.patch' | LC_ALL=C sort); do
        patch -p1 --forward --batch < "$p"
      done
    fi
    go mod download
    rm -rf $GOMODCACHE/cache/download/sumdb
    cp -r $GOMODCACHE/cache/download $out
  '';
}
