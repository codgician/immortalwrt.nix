# Turn a `fetch` entry from a channel lock into a fixed-output derivation. `narHash` is what
# `nix flake prefetch` reports, which is the same NAR hash these fetchers verify.
{ pkgs }:
fetch:
if fetch.type == "github" then
  pkgs.fetchFromGitHub {
    inherit (fetch) owner repo rev;
    hash = fetch.narHash;
  }
else if fetch.type == "git" then
  pkgs.fetchgit {
    inherit (fetch) url rev;
    hash = fetch.narHash;
  }
else
  throw "immortalwrt.nix: unsupported fetch type '${fetch.type}'"
