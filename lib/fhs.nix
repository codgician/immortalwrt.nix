# OpenWrt assumes a Debian-like host: rules.mk runs every recipe through `/usr/bin/env bash`,
# and extracted tarballs carry `#!/usr/bin/env` and `#!/bin/bash` shebangs. Rather than patching
# each tree, run the buildroot inside a tiny FHS view that adds exactly those paths.
#
# Usage: owrt-fhs <writable dir> <command...>
# Works inside the Nix sandbox as long as nested unprivileged user namespaces are allowed.
{ pkgs }:
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
    --ro-bind ${pkgs.bash}/bin/bash /bin/sh \
    --ro-bind ${pkgs.bash}/bin/bash /bin/bash \
    --ro-bind ${pkgs.coreutils}/bin/env /usr/bin/env \
    --chdir "$PWD" \
    -- "$@"
''
