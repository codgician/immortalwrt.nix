{
  description = "ImmortalWrt source channels, pinned and built from source with Nix";

  nixConfig = {
    extra-substituters = [ "https://cache.codgician.me/immortalwrt" ];
    extra-trusted-public-keys = [ "immortalwrt:t17gIHmdp6G9DpukeaW0LCB32Lg+mPRvN0SNdGW1r90=" ];
  };

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      # The OpenWrt buildroot only supports building on Linux; x86_64 is what we exercise.
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      inherit (nixpkgs) lib;
      inherit (import ./lib { inherit pkgs lib; }) mkChannel;
      inherit (import ./channels { inherit lib mkChannel; }) channels aliases;
    in
    {
      legacyPackages.${system} = {
        # channels.<name>.{tree, metadata, mkImages, mkImage, imagebuilder, toolchain, ...}
        channels = channels // aliases;
      };

      packages.${system} =
        lib.concatMapAttrs (name: channel: {
          "${name}-tree" = channel.tree;
          "${name}-metadata" = channel.metadataDrv;
        }) channels
        # A device that only exists in a fork: Gemtek / Brightspeed XR1710G, for the stock
        # partition layout and the OpenWrt U-Boot UBI layout, sharing one ImageBuilder.
        // channels.gemtek.mkImages (
          let
            device = profile: {
              target = "airoha/an7581";
              inherit profile;
              packages = [
                "luci"
                "luci-i18n-base-zh-cn"
                "luci-i18n-firewall-zh-cn"
                "luci-app-package-manager"
                "luci-i18n-package-manager-zh-cn"
                "luci-app-upnp"
                "luci-i18n-upnp-zh-cn"
                "luci-proto-wireguard"
                "luci-app-airoha-npu"
                "luci-app-airoha-factory"
                "luci-app-airoha-fancontrol"
                "luci-app-airoha-recovery"
                "luci-app-mlo"
                "luci-app-usteer"
                "luci-app-wifischedule"
              ];
            };
          in
          {
            gemtek-xr1710g = device "gemtek_xr1710g";
            gemtek-xr1710g-ubi = device "gemtek_xr1710g-ubi";
          }
        )
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
            lib.filterAttrs (
              _: c:
              c.metadata == null
              || c.sources == null
              || c.metadata.revision != c.revision
              || c.sources.revision != c.revision
            ) channels
          );
        in
        assert lib.assertMsg (stale == [ ])
          "metadata.json or sources.json does not describe the locked revision of: ${lib.concatStringsSep ", " stale}; run `nix run .#refresh`";
        pkgs.runCommand "metadata-matches-lock" { } "touch $out";

      formatter.${system} = pkgs.nixfmt;
    };
}
