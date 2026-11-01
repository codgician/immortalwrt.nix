{
  description = "ImmortalWrt source channels, pinned and built with Nix";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # official ImageBuilders and package repositories with daily-pinned hashes (release images)
    imagebuilder = {
      url = "github:codgician/nix-immortalwrt-imagebuilder";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      imagebuilder,
      ...
    }:
    let
      # The OpenWrt buildroot only supports building on Linux; x86_64 is what we exercise.
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      inherit (nixpkgs) lib;
      iwrt = import ./lib { inherit pkgs lib imagebuilder; };
      inherit
        (import ./channels {
          inherit lib;
          inherit (iwrt) mkChannel;
        })
        channels
        aliases
        ;
    in
    {
      legacyPackages.${system} = {
        # channels.<name>.{tree, metadata, metadataDrv, lock, version, revision, ...}
        channels = channels // aliases;
        # releases the image backend can build; the refresh keeps release channels within these
        inherit (iwrt) binaryReleases;
      };

      packages.${system} =
        lib.concatMapAttrs (name: channel: {
          "${name}-tree" = channel.tree;
          "${name}-metadata" = channel.metadataDrv;
        }) channels
        // {
          refresh = pkgs.writeShellApplication {
            name = "refresh";
            # `nix` comes from the caller's PATH so the refresh talks to the same daemon.
            runtimeInputs = with pkgs; [
              python3
              git
              coreutils
            ];
            text = ''exec python3 ${./scripts/refresh.py} "$@"'';
          };
        };

      checks.${system}.metadata-matches-lock =
        let
          stale = lib.attrNames (
            lib.filterAttrs (_: c: c.metadata == null || c.metadata.revision != c.revision) channels
          );
        in
        assert lib.assertMsg (stale == [ ])
          "metadata.json does not describe the locked revision of: ${lib.concatStringsSep ", " stale}; run `nix run .#refresh`";
        pkgs.runCommand "metadata-matches-lock" { } "touch $out";

      formatter.${system} = pkgs.nixfmt;
    };
}
