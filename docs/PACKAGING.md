# Packaging

Kukni does not have a published package repository yet. For a complete Ubuntu
runtime, use the tested alpha `.deb` with its required dependencies and namespace
setup. Main-branch CI retains the exact successfully tested package and checksum
for 30 days; it does not publish a stable or signed release. Development runs
directly from the checkout with `make dev`, never by copying an installation.

## Legacy per-user layout (migration only)

The old source copier used this layout below the current user's home:

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

This layout is now deprecated: its activation files can hide a newer system
package indefinitely. Plain `./install.sh` refuses to create it. The tested
implementation is retained behind explicit `--legacy-user-install` for legacy
compatibility only, not recommended development. `./uninstall.sh --dry-run`
performs the same ownership preflight as removal but changes no installed files.
Neither the copier nor development mode installs system AppArmor policy.

## Local Ubuntu alpha package

`packaging/build-deb.py` builds from a clean commit with full Git history, derives
a monotonic alpha version from the Git commit count, and writes the artifact
below ignored `dist/`. Shallow histories are rejected rather than emitting a
lower version for newer code. CI uses `fetch-depth: 0`. Building and inspecting it require no privilege and make no system
changes:

```sh
./packaging/build-deb.py
# Substitute the exact filename printed by the builder:
dpkg-deb --info dist/kukni_VERSION_all.deb
dpkg-deb --contents dist/kukni_VERSION_all.deb
/usr/sbin/apparmor_parser --skip-kernel-load --skip-cache \
  packaging/debian/io.github.lamosty.Kukni.apparmor
```

The package declares `Conflicts` and `Replaces` for `gnome-sushi`, because both
provide the well-known Nautilus previewer D-Bus service. Installing Kukni can
therefore remove Sushi; restoring Sushi later requires reinstalling the
`gnome-sushi` package rather than merely removing Kukni.

### Migrate from the per-user preview

Per-user D-Bus activation and desktop files take precedence over system files,
and `~/.local/bin/kukni` can take precedence in the shell. Package installation
must not delete home-directory files as root. This is a one-time user migration:

1. Install the `.deb` with APT and confirm that installation completed. Keeping
   the old user copy until this succeeds avoids losing a working previewer if
   package installation fails.
2. Close the running preview window. As the normal desktop user run the old
   installed `~/.local/lib/kukni/uninstall.sh`, without sudo or `--force`. A
   custom-prefix installation must use its own installed uninstaller instead.
   From a current checkout, `./uninstall.sh --dry-run` provides a no-change
   preflight for the default installation.
3. Run `/usr/bin/kukni --check` explicitly. Images and PDF must render real
   synthetic content. Shadowing launchers/activation are required failures;
   optional HTML checks only report prerequisites.
4. Test Space in Nautilus. If an old process still owns the service, close it;
   sign out/in if necessary. Updating activation files does not replace a
   running process.

Diagnostics inspect standard activation precedence, including custom XDG
locations, and query existing D-Bus owners without activating apps. Unavailable
session-bus inspection is reported honestly rather than claimed as a live
activation test. No private documents, service contents, environment values, or
raw process command lines are printed. `make dev` is explicitly excluded from
production-name ownership and never changes this setup.

### AppArmor conffile lifecycle

The package owns `/etc/apparmor.d/io.github.lamosty.Kukni` as a conffile. Its
unconfined attachment to the root-owned `/usr/lib/kukni/launcher/kukni` grants
only the `userns` eligibility needed to start Bubblewrap and WebKit sandboxes.
It is **not** a renderer sandbox: those process sandboxes remain mandatory.
This launcher/profile arrangement is also not protection against a malicious
process or environment already controlled by the same user.

If an administrator deletes the conffile, package configuration preserves that
decision and prints a warning instead of silently recreating or overriding it.
Use the package manager's explicit conffile-recovery mechanism when restoration
is intended. Plain removal retains conffiles; `sudo apt purge kukni` performs
complete package configuration/profile removal. A subsequent install creates a
fresh packaged profile.

## Current CI download channel

After unit, GTK, and installed-runtime checks succeed, main-branch push jobs
upload `kukni-ubuntu-24.04-<commit>` with the exact tested `.deb` and
`SHA256SUMS`. Downloads require GitHub sign-in and expire after 30 days. This
avoids requiring every tester to clone and build, but is not a stable release
channel, signed package provenance, or automatic update service. Only the
package and checksum are uploaded, never the checkout or test documents.

The next public distribution milestone is a versioned GitHub Release download
with reviewable release notes, followed by a signed APT channel. No release,
repository account, signing credential, or deployment is created by the build.

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
