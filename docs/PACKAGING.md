# Packaging

Kukni does not have a published package repository yet. Today, the supported
installation path is the repository's conflict-checked per-user `install.sh`.
This document describes the path from that alpha installer to normal Linux
packages; it does not claim that those packages are already available.

## Current per-user layout

With the defaults, `./install.sh` writes only below the current user's home:

```text
~/.local/bin/kukni
~/.local/lib/kukni/
~/.local/share/applications/io.github.lamosty.Kukni.desktop
~/.local/share/dbus-1/services/io.github.lamosty.Kukni.service
~/.local/share/dbus-1/services/org.gnome.NautilusPreviewer.service
```

The private application directory contains the Python sources, workers,
licenses, ownership manifest, and installed uninstaller. The launcher in
`~/.local/bin` is a relative link into that directory. `PREFIX` and
`XDG_DATA_HOME` can select other absolute, non-system locations.

The installer must not run as root. It stages files before replacement, records
hashes and modes, refuses unexpected conflicts by default, and reloads the
session-bus activation configuration on a best-effort basis. The uninstaller
removes only manifest-owned files that still match their recorded state unless
the user explicitly chooses `--force` after review.

This layout makes a source checkout usable, but it is not a substitute for a
versioned distro package: it does not resolve dependencies, provide automatic
updates, or install a system AppArmor profile.

## Ubuntu release path

### 1. Tagged source release

The first public release should include:

- a SemVer tag and release notes;
- a source archive generated from the tag;
- SHA-256 checksums and a signed tag;
- clean-install, upgrade, and uninstall results from CI;
- an explicit supported Ubuntu/Nautilus version matrix.

### 2. Reviewable `.deb`

Build a conventional Debian package from the same tagged source. It should own
Kukni's executable, Python package, helpers, desktop and AppStream metadata,
icons, D-Bus activation files, licenses, and any narrowly scoped AppArmor policy
required by sandboxed renderers. Package metadata must declare exact runtime
requirements rather than asking users to install them manually.

The package also needs an explicit policy for the well-known Nautilus previewer
service currently provided by `gnome-sushi`. A `.deb` must either declare and
test the appropriate conflict/replacement relationship or use a future
file-manager integration that avoids the shared path. It must never overwrite
another package's activation file behind the package manager's back.

### 3. Launchpad PPA

APT does not have an application-name registration step. Users can run
`apt install kukni` only after a `kukni` Debian package is published by Ubuntu,
Debian, or a configured third-party repository.

The practical early channel is a signed Launchpad PPA. Publishing there requires
a Launchpad account, a PPA, and uploaded source packages; users then add that PPA
and receive Kukni through normal APT install and update behavior. See
[Launchpad's PPA reference](https://documentation.ubuntu.com/launchpad/user/reference/packaging/ppas/ppa/)
and [PPA installation guide](https://documentation.ubuntu.com/launchpad/user/how-to/packaging/ppa-install/).

A PPA should follow, not precede, a locally verified `.deb` and stable upgrade
layout. Inclusion in Debian or Ubuntu can be pursued later through their normal
review and release processes.

## Other distribution formats

### RPM and community packages

After the install layout and release process stabilize, keep Fedora/openSUSE
spec files and Arch packaging thin: build from signed source tags, declare
runtime dependencies in the native package manager, and avoid mutable branch
archives.

### Flatpak

Flatpak remains worth evaluating for direct-launch previews. Nautilus integration
and arbitrary selected-file access may require a small, separately audited host
adapter or a suitable portal. Do not claim full Space-key integration until the
host/sandbox boundary is tested on a clean installation.

### Snap

Snap is not the first target. Publishing requires registration of a globally
unique Snap name, but registration is not the main technical blocker; strict
confinement must also support the session D-Bus contract, Nautilus activation,
selected-file access, and disposable renderer workers. See
[Snap name registration](https://snapcraft.io/docs/registering-your-app-name/).

Kukni should not request classic confinement merely to make packaging easy. A
Snap can be reconsidered after the standalone `.deb` and PPA path is working and
the strict-confinement integration has a credible test matrix.

## Sandbox packaging requirement

Ubuntu can restrict unprivileged user namespaces for unconfined applications.
Any package that enables bubblewrap-backed PDF, HTML, or media workers must ship
and test the narrow policy required for those namespaces. The policy must not
grant broad filesystem or network access, and missing policy must lead to a
fallback rather than a sandbox bypass.

## Release requirements

- Reproducible package jobs where the ecosystem permits.
- Pinned CI actions with least-privilege workflow permissions.
- Checksums, signed tags, a software bill of materials, and provenance
  attestations.
- Tests for clean install, upgrade, rollback, uninstall, D-Bus activation, and
  coexistence or migration from another preview provider.
- Automated secret, license, and dependency scanning.
- No private photographs, document contents, or third-party RAW samples in
  source or package artifacts.
