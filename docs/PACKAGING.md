# Packaging

Kukni should be easy to install without asking users to pipe a downloaded script into a root shell. Every distribution format should be generated from one canonical Meson install layout.

## Canonical installed components

The standalone app will define stable locations for:

- the Kukni executable and Python package;
- extractor and converter workers;
- a `.desktop` launcher;
- AppStream metadata and icons;
- D-Bus activation and interface files;
- GSettings schemas, translations, and MIME metadata when introduced;
- an optional, minimal host adapter for sandboxed packages.

Packages must not overwrite the system GNOME Sushi activation file. A user-level compatibility adapter must be explicit, reversible, and conflict-checked.

## Release progression

### 1. Source and release artifacts

The first tagged release should provide a source archive, SHA-256 checksums, a signed tag, and CI-built `.deb` and `.rpm` packages. The existing per-user installer remains useful for the legacy Sushi adapter but is not the long-term application installer.

### 2. Ubuntu and Debian

A `.deb` attached to GitHub Releases gives users a reviewable first package. After the file layout and upgrade behavior stabilize, maintain a signed APT repository so users can install and update with their package manager. Inclusion in Debian—and later Ubuntu through synchronization—is desirable but requires downstream review and follows distribution release timelines.

### 3. Fedora, openSUSE, and Arch

Build RPMs from the same Meson layout and keep distro spec files thin. Provide downstream packaging notes and source checksums. An AUR `PKGBUILD` should build from a signed source tag rather than download mutable branch content.

### 4. Flatpak and Flathub

Flatpak is the primary cross-distribution target for the standalone viewer. The viewer and risky renderers should stay sandboxed. If Nautilus requires host integration that Flatpak cannot provide directly, ship a tiny separately audited adapter whose only job is to pass the selected document through a portal or narrow D-Bus contract.

### 5. Snap feasibility

Snap is useful on Ubuntu, but strict confinement must be proven for session D-Bus ownership, file-manager integration, arbitrary selected-file access, and subprocess converters. Kukni should not request classic confinement merely to make packaging easy. If those constraints cannot be satisfied cleanly, the `.deb` and Flatpak remain the recommended Ubuntu channels.

## Release requirements

- Versioned changelog and SemVer tags.
- Pinned CI actions with least-privilege workflow permissions.
- Reproducible package jobs where the ecosystem permits.
- Checksums, signed tags, software bill of materials, and provenance attestations.
- Clean-install, upgrade, rollback, and uninstall tests.
- Automated secret, license, and dependency scanning.
- No private photographs or third-party RAW samples in source or package artifacts.
