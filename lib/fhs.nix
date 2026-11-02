# OpenWrt assumes a Debian-like host: rules.mk runs every recipe through `/usr/bin/env bash`,
# extracted tarballs carry `#!/usr/bin/env` and `#!/bin/bash` shebangs, some packages call host
# tools by absolute path (perl's cross configuration uses `/bin/ln -s`), the Go bootstrap runs PIE
# executables that expect the system's dynamic loader, and some configure scripts look for host
# directories such as /var/lock (minicom). Rather than patching each tree, run the buildroot inside
# a tiny FHS view that adds exactly those paths: a /bin with bash and coreutils, /usr/bin/env, the
# glibc loader and /var/lock.
#
# Usage: owrt-fhs <writable dir> <command...>
# Works inside the Nix sandbox as long as nested unprivileged user namespaces are allowed.
{ pkgs }:
let
  bin = pkgs.runCommand "owrt-fhs-bin" { } ''
    mkdir -p $out/bin
    ln -s ${pkgs.coreutils}/bin/* $out/bin/
    ln -s ${pkgs.bash}/bin/bash $out/bin/bash
    ln -s ${pkgs.bash}/bin/bash $out/bin/sh
  '';
in
pkgs.writeShellScriptBin "owrt-fhs" ''
  rw=$1
  shift
  exec ${pkgs.bubblewrap}/bin/bwrap \
    --bind "$rw" "$rw" \
    --ro-bind /nix /nix \
    --dev /dev \
    --proc /proc \
    --ro-bind /etc /etc \
    --bind /tmp /tmp \
    --ro-bind ${bin}/bin /bin \
    --ro-bind ${pkgs.coreutils}/bin/env /usr/bin/env \
    --ro-bind ${pkgs.glibc}/lib /lib64 \
    --dir /var/lock \
    --chdir "$PWD" \
    -- "$@"
''
