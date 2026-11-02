#!/usr/bin/env python3
"""Turn the buildroot scan (tmp/.packageinfo, tmp/.targetinfo) into channel metadata JSON.

Run from the tree root after `make prepare-tmpinfo`. Besides names (checked at evaluation
time), the metadata carries the package dependency graph so that a build can be handed exactly
the sources it needs, without evaluating Kconfig. Output is one entry per line with sorted keys
so the daily refresh produces small git diffs.
"""

import glob
import json
import os
import pathlib
import re
import sys


def stanzas(path):
    with open(path, encoding="latin-1") as f:
        for block in f.read().split("\n@@\n"):
            fields = {}
            for line in block.splitlines():
                key, sep, value = line.partition(": ")
                if sep and key and " " not in key and key not in fields:
                    fields[key] = value.strip()
            yield fields


def canonical_dir(makefile):
    # The scan reaches some Makefiles through package/feeds symlinks, and which path it reports
    # depends on directory order; record the real location to stay deterministic.
    return os.path.relpath(os.path.realpath(makefile)).removesuffix("/Makefile")


def dependency_names(spec):
    """Package names in a DEPENDS-style list. Guards (COND:pkg) are kept, conditions (@SYMBOL)
    dropped: the result over-approximates what Kconfig selects, which is what a source set needs."""
    names = set()
    for token in spec.split():
        token = token.lstrip("+")
        if ":" in token:
            names.add(token.rsplit(":", 1)[-1].lstrip("+"))
        elif token and not token.startswith(("@", "!")):
            names.add(token)
    return names


def required_names(spec):
    """Unconditional dependencies that Kconfig does not select (no "+"): a package is only
    enabled once these are, so a request has to include them."""
    return {t for t in spec.split() if t and not t.startswith(("+", "@", "!")) and ":" not in t}


# Symbols a package default can depend on that a source closure can evaluate (see lib/sources.nix)
DEFAULT_SYMBOL = re.compile(r"\b(?:TARGET_[\w.\-]+|PACKAGE_[\w.\-+]+|BUILDBOT)")
# Go packages (golang-package.mk, but not the Go compilers themselves) fetch their modules at
# build time; those need vendored modules (lib/vendor.nix)
GO_PACKAGE = re.compile(r"^include .*/golang-package\.mk$", re.M)
GO_COMPILER = re.compile(r"^include .*/golang-compiler\.mk$", re.M)


def default_entry(spec, depends):
    """`True` for an unconditional default, else the symbols the default depends on (or None).

    Unconditional defaults of target-specific packages (bootloaders, `DEPENDS:=@TARGET_...`)
    are narrowed to those targets."""
    value, _, condition = spec.partition(" if ")
    if value.strip() == "n":
        return None
    if value.strip() in ("y", "m") and not condition:
        guards = " ".join(d for d in depends.split() if d.startswith("@"))
        return sorted(set(re.findall(r"(?<![!\w])(TARGET_[\w.\-]+)", guards))) or True
    return sorted(set(DEFAULT_SYMBOL.findall(value + " " + condition))) or None


def build_edges(path):
    """Directory-level build dependencies from tmp/.packagedeps, as the buildroot's make sees them.

    The buildroot compiles a directory when another depends on it, whichever of its packages are
    selected: util-linux depends on ncurses because cfdisk does, so ncurses is built even when
    nothing selects libncurses. Conditions (`$(if $(CONFIG_...),...)`) are taken as true."""
    edges = {}
    for line in pathlib.Path(path).read_text(encoding="latin-1").splitlines():
        m = re.match(r"\$\(curdir\)/(\S+?)/(?:host/)?compile \+= (.*)$", line)
        if not m:
            continue
        source = canonical_dir(f"package/{m[1]}/Makefile")
        for dep in re.findall(r"\$\(curdir\)/(\S+?)/(?:host/)?compile", m[2]):
            edges.setdefault(source, set()).add(canonical_dir(f"package/{dep}/Makefile"))
    return {source: deps - {source} for source, deps in edges.items()}


def packages_and_sources(path):
    packages, sources, current = {}, {}, None
    for st in stanzas(path):
        if "Source-Makefile" in st:
            makefile = st["Source-Makefile"]
            current = canonical_dir(makefile)
            text = pathlib.Path(makefile).read_text(encoding="latin-1")
            if GO_PACKAGE.search(text) and not GO_COMPILER.search(text):
                sources[current] = {"go": True}
        name = st.get("Package")
        if not name:
            continue
        entry = {"source": current}
        depends = sorted(dependency_names(st.get("Depends", "")) - {name})
        if depends:
            entry["depends"] = depends
        requires = sorted(required_names(st.get("Depends", "")) - {name})
        if requires:
            entry["requires"] = requires
        if "Hidden" in st:
            entry["hidden"] = True
        # "@" marks a virtual name, which dependencies use without it
        provides = {p.lstrip("@") for p in st.get("Provides", "").split()} - {name}
        if provides:
            entry["provides"] = sorted(provides)
        default = default_entry(st.get("Default", "n"), st.get("Depends", ""))
        if default:
            entry["default"] = default
        packages[name] = entry
    for directory, deps in build_edges(os.path.join(os.path.dirname(path), ".packagedeps")).items():
        if deps:
            sources.setdefault(directory, {})["buildDepends"] = sorted(deps)
    return packages, sources


def targets(path):
    result, current = {}, None
    for st in stanzas(path):
        if "Target" in st:
            current = st["Target"]
            result[current] = {
                # Kconfig's architecture symbol, e.g. aarch64
                "arch": st.get("Target-Arch", ""),
                "linux": st.get("Linux-Version", ""),
                "defaultPackages": [],
                "profiles": {},
            }
        if not current:
            continue
        if "Default-Packages" in st:
            result[current]["defaultPackages"] = sorted(set(st["Default-Packages"].split()))
        profile = st.get("Target-Profile", "")
        if profile.startswith("DEVICE_"):
            pkgs = result[current]["profiles"].setdefault(profile.removeprefix("DEVICE_"), [])
            pkgs[:] = sorted(set(pkgs) | set(st.get("Target-Profile-Packages", "").split()))
    return {name: info for name, info in result.items() if info["profiles"]}


def luci_languages(path):
    """Map the language suffix of luci-i18n-* packages to its LUCI_LANG_* option."""
    if not os.path.exists(path):
        return {}
    text = pathlib.Path(path).read_text(encoding="utf-8")
    aliases = dict(re.findall(r"^LUCI_LC_ALIAS\.(\S+)=(\S+)", text, re.M))
    langs = re.findall(r"^LUCI_LANG\.(\S+)=", text, re.M)
    return {aliases.get(lang, lang.lower().replace("_", "-")): lang for lang in langs}


KCONFIG_ENTITIES = {
    "config",
    "menuconfig",
    "choice",
    "endchoice",
    "if",
    "endif",
    "menu",
    "endmenu",
    "comment",
}
KCONFIG_ARCHES = {
    "aarch64",
    "aarch64_be",
    "arc",
    "arceb",
    "arm",
    "armeb",
    "i386",
    "loongarch64",
    "mips",
    "mips64",
    "mips64el",
    "mipsel",
    "powerpc",
    "powerpc64",
    "riscv64",
    "x86_64",
}
KCONFIG_SYMBOL = re.compile(r"[A-Za-z0-9_][\w.\-+]*")


def kconfig_options(text):
    """The `config` options of a Kconfig fragment with the packages they select, the defaults
    that turn them on (as conditions, None meaning always) and every condition they sit under."""
    options, frames, owner, in_help = [], [], None, False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        word, _, rest = line.partition(" ")
        rest = rest.strip()
        if in_help and word not in KCONFIG_ENTITIES:
            continue
        in_help = False
        if word in ("config", "menuconfig"):
            choice = frames[-1] if frames and frames[-1]["kind"] == "choice" else None
            owner = {
                "name": rest,
                "selects": [],
                "symbols": [],
                "defaults": [],
                "conds": [],
                "frames": list(frames),
            }
            options.append(owner)
            if choice is not None:
                owner["choice"] = choice
                choice["members"].append(rest)
        elif word in ("choice", "menu", "if"):
            owner = {"kind": word, "conds": [rest] if word == "if" else [], "defaults": [], "members": []}
            frames.append(owner)
            owner = owner if word != "if" else None
        elif word in ("endchoice", "endmenu", "endif"):
            frames and frames.pop()
            owner = None
        elif word == "comment":
            owner = None
        elif line in ("help", "---help---"):
            in_help = True
        elif owner is None:
            continue
        elif line.startswith("depends on "):
            owner["conds"].append(line.removeprefix("depends on "))
        elif word == "default":
            owner["defaults"].append(rest)
        elif word == "select" and "selects" in owner:
            symbol = rest.split()[0]
            owner["symbols"].append(symbol)
            if symbol.startswith("PACKAGE_"):
                owner["selects"].append(symbol.removeprefix("PACKAGE_"))
    for option in options:
        choice = option.get("choice")
        if choice is None:
            defaults = [d.partition(" if ") for d in option["defaults"]]
            enabling = [
                cond or (None if value in ("y", "m") else value)
                for value, _, cond in defaults
                if value != "n"
            ]
        elif choice["defaults"]:
            defaults = [d.partition(" if ") for d in choice["defaults"]]
            enabling = [cond or None for value, _, cond in defaults if value == option["name"]]
        else:  # Kconfig picks the first member of a choice without default
            enabling = [None] if choice["members"][:1] == [option["name"]] else []
        option["enabling"] = enabling
        option["conds"] = option["conds"] + [c for f in option["frames"] for c in f["conds"]]
    return options


def possible(expr, defined, on):
    """Whether a condition may hold: a plain conjunction fails on an option known to stay off."""
    if "||" in expr:
        return True
    for term in expr.replace("(", " ").replace(")", " ").split("&&"):
        term = term.strip()
        if term in defined and term not in on:
            return False
    return True


def config_selects(packageinfo, packages, target_symbols):
    """Packages that Kconfig options can select besides package dependencies.

    These are the buildroot's own options (image formats select kmod-e1000 and grub2) and package
    config options (`Package/<name>/config`), counting options that may end up on: those with a
    default that turns them on, or selected by such options, unless a condition names an option
    that stays off. An option that depends on its package becomes a dependency of that package.
    Kconfig does not nest the others under anything: luci-app-ssr-plus selects 3proxy whether or
    not it is built, so those are returned with the architectures and targets their defaults and
    dependencies name, for source closures to include."""
    blocks = []
    for kconfig in sorted(
        {*glob.glob("config/*.in"), "Config.in", *glob.glob("target/**/Config.in", recursive=True)}
    ):
        if os.path.isfile(kconfig):
            blocks.append((None, pathlib.Path(kconfig).read_text(encoding="latin-1")))
    with open(packageinfo, encoding="latin-1") as f:
        name = None
        for block in f.read().split("\n@@\n"):
            if m := re.search(r"^Package: (\S+)", block, re.M):
                name = m[1]
            elif block.lstrip("\n").startswith("Config:"):
                blocks.append((name, block.lstrip("\n").removeprefix("Config:")))
    options = [(package, option) for package, text in blocks for option in kconfig_options(text)]
    defined = {option["name"] for _, option in options}
    # set by every build (lib/build.nix)
    on, selected, changed = {"BUILDBOT", "IB", "IB_STANDALONE", "TARGET_MULTI_PROFILE"}, set(), True
    while changed:
        changed = False
        for _, option in options:
            if option["name"] in on or not all(possible(c, defined, on) for c in option["conds"]):
                continue
            if option["name"] in selected or any(
                e is None or possible(e, defined, on) for e in option["enabling"]
            ):
                on.add(option["name"])
                selected.update(option["symbols"])
                changed = True
    unguarded = {}
    for package, option in options:
        if not option["selects"] or option["name"] not in on:
            continue
        symbols = {s for c in option["conds"] for s in re.findall(r"(?<![!\w])([A-Za-z_][\w.\-+]*)", c)}
        if package is not None and f"PACKAGE_{package}" in symbols:
            entry = packages[package]
            entry["depends"] = sorted(set(entry.get("depends", [])) | set(option["selects"]) - {package})
            continue
        gate = {}
        arches = {s for c in option["enabling"] if c for s in KCONFIG_SYMBOL.findall(c)} - {"y", "m"}
        if option["enabling"] and None not in option["enabling"] and arches and arches <= KCONFIG_ARCHES:
            gate["arches"] = sorted(arches)
        targets = symbols & target_symbols
        if targets:
            gate["targets"] = sorted(targets)
        key = json.dumps(gate, sort_keys=True)
        unguarded.setdefault(key, set()).update(option["selects"])
    return [dict(json.loads(key), packages=sorted(selected)) for key, selected in sorted(unguarded.items())]


def dump_section(name, entries, last=False):
    lines = [
        f"  {json.dumps(key)}: {json.dumps(value, sort_keys=True)}" for key, value in sorted(entries.items())
    ]
    return f'  "{name}": {{\n' + ",\n".join(f"  {line}" for line in lines) + "\n  }" + ("" if last else ",")


def main():
    packageinfo, targetinfo, revision = sys.argv[1:4]
    packages, sources = packages_and_sources(packageinfo)
    target_info = targets(targetinfo)
    target_symbols = {
        "TARGET_" + symbol.replace("/", "_").replace("-", "_")
        for name in target_info
        for symbol in (name.split("/")[0], name)
    }
    selects = config_selects(packageinfo, packages, target_symbols)
    print("{")
    print(f'  "revision": {json.dumps(revision)},')
    print(
        '  "configSelects": [\n'
        + ",\n".join(f"    {json.dumps(s, sort_keys=True)}" for s in selects)
        + "\n  ],"
    )
    print(dump_section("luciLanguages", luci_languages("feeds/luci/luci.mk")))
    print(dump_section("targets", target_info))
    print(dump_section("sources", sources))
    print(dump_section("packages", packages, last=True))
    print("}")


if __name__ == "__main__":
    main()
