# Host tools the OpenWrt buildroot expects (include/prereq-build.mk) plus what its scripts call.
{ pkgs }:
with pkgs;
[
  gnumake
  perl
  (python3.withPackages (ps: [ ps.setuptools ]))
  git
  wget
  unzip
  bzip2
  xz
  zstd
  file
  rsync
  which
  getopt
  gawk
  gettext
  libxslt
  ncurses
  zlib
  openssl
  pkg-config
  bc
  cpio
  swig
  quilt
  diffutils
  patch
  findutils
  gnugrep
  gnused
  gnutar
  gzip
  # mkisofs and qemu-img, for x86 ISO, QCOW2, VDI, VMDK and VHDX images (target/linux/x86)
  cdrkit
  qemu-utils
]
