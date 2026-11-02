# pietoolchain workflows

Reusable GitHub workflows that build pietoolchain packaging repositories
and publish their packages. They cross-compile each package for
darwin-arm64 with [pietoolchain](https://github.com/blueberrypieos/pietoolchain).

## Using it

Add this as the packaging repository's `.github/workflows/build.yml`:

```yaml
name: build
on:
  push:
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  build:
    uses: blueberrypieos/workflows/.github/workflows/build.yml@v1
  attest:
    needs: build
    if: >-
      github.event_name == 'push' &&
      (github.ref_type == 'tag' ||
       github.ref == format('refs/heads/{0}', github.event.repository.default_branch))
    permissions:
      contents: read
      id-token: write
      attestations: write
      artifact-metadata: write
    uses: blueberrypieos/workflows/.github/workflows/attest.yml@v1
    with:
      artifact: ${{ needs.build.outputs.artifact }}
  release:
    needs: [build, attest]
    if: github.event_name == 'push' && github.ref_type == 'tag'
    permissions:
      contents: write
    uses: blueberrypieos/workflows/.github/workflows/release.yml@v1
    with:
      artifact: ${{ needs.build.outputs.artifact }}
```

Only the jobs that need write permissions get them, and only on the runs
that need them: every run builds with `contents: read`; a push of the
default branch (`blueberry-pie-os/latest`) or of a tag also attests; a tag
push also releases. `release.yml`'s job runs in the repository's
`release` environment, whose protection rules can require approval and
limit which tags may release. Every action is pinned to a commit.

The workflows run on GitHub's Ubuntu 26.04 runner, against the current
Ubuntu archive; each package's `.buildinfo` records the host packages it
was built with.

`build.yml` has two jobs.

The `source` job:

1. on a tag, checks that it is the changelog version's package tag, and
   stops otherwise;
2. makes the source package with `tools/pie-source source`:
   - the orig tarball comes from the upstream tag (`upstream-tag` in
     `debian/gbp.conf`; a `+dsN` version uses the tag of the version
     without the suffix), or from the `pristine-tar` branch when
     `debian/gbp.conf` sets `pristine-tar = True`;
   - it keeps only the files that `debian/copyright`'s `Files-Excluded`
     and `Files-Included` leave in, as `mk-origtargz` does;
   - `debian/` comes from the commit being built, and nothing else may
     differ from the upstream tag;
3. checks the source package with `tools/pie-verify check`: lintian's
   copyright tags; `debian/copyright` against every file in the package,
   with no unused pattern or licence; licensecheck through `lrc`; the
   restricted-text markers; and files for other architectures;
4. uploads it as the artifact `SOURCE_VERSION_source`.

The `binary` job runs in a new `ubuntu:26.04` container, with no checkout:

1. downloads pietoolchain and pietoolchain-sdk from the pietoolchain
   release it pins, checks their SHA-256 hashes and installs them;
2. extracts the source package with `dpkg-source -x`;
3. installs the build dependencies (`apt-get build-dep`);
4. builds the binary packages from the extracted source alone:

   ```sh
   dpkg-buildpackage -a darwin-arm64 -b -us -uc -j$(nproc)
   ```

5. merges the source and binary `.changes`, and collects every file it
   lists: the source package (`.dsc`, `.orig.tar.*`, `.debian.tar.*`), the
   `.deb` of each binary package, the `.buildinfo`, and the `.changes`
   itself;
6. uploads them as one artifact, named `SOURCE_VERSION` (the version
   without its epoch; `outputs.artifact` gives the name).

A file the source package leaves out cannot reach a binary package: the
binary job never sees the git tree.

`attest.yml` attests their build provenance. It signs the attestations, so
check a package with:

```sh
gh attestation verify FILE --repo OWNER/REPO \
  --signer-workflow blueberrypieos/workflows/.github/workflows/attest.yml
```

`release.yml` publishes them as the tag's GitHub release.

The workflow does not run tests.


### Ports that build against other ports

A port whose Build-Depends name other ports' `-dev` packages passes the
ports' apt repository to its `build` job:

```yaml
    uses: blueberrypieos/workflows/.github/workflows/build.yml@v1
    with:
      ports-repository: https://example.org/debian blueberry-pie-os
      ports-keyring-url: https://example.org/debian/repository.key
```

The build then:

- adds the darwin-arm64 architecture and that repository;
- runs pietoolchain's `pie-cross-deps`, which installs the build tools and
  unpacks the other ports' darwin-arm64 packages into
  `/var/lib/pietoolchain/cross-root`;
- builds with `dpkg-buildpackage -a darwin-arm64 -d`.

The ports' `Depends` on core's libraries resolve against pietoolchain-sdk,
which provides core's runtime packages at the versions it was built with.
Build tools in Build-Depends must be `Multi-Arch: foreign`,
`Architecture: all` or `:native`, and the port needs
`Build-Depends: pietoolchain (>= 0.1.0)`, for `pie-cross-deps`.

## What a packaging repository contains

The upstream tree is on the upstream branch, `main`, with its release
tags. The `blueberry-pie-os/latest` branch has that tree plus:

- `debian/gbp.conf`, with the DEP-14 branch and tag names
  (`upstream-branch`, `upstream-tag`,
  `debian-branch = blueberry-pie-os/latest`,
  `debian-tag = blueberry-pie-os/%(version)s`).
- `debian/control`, with `Build-Depends: pietoolchain (>= VERSION)` (plus
  any host tools the port needs) and its binary packages, each with
  `Architecture: darwin-arm64`. With more than one, each after the first
  has a `debian/PACKAGE.files` naming the files it takes (a library's
  `-dev` package: `usr/include/*`, say).
- `debian/rules`, which includes `/usr/share/pietoolchain/rules.mk` and
  then has the port's build recipe, with an `install` target that
  installs into `$(ROOT)`. The header of pietoolchain's `rules.mk`
  describes what it provides.
- `debian/copyright`. Its first paragraph has `Files-Excluded: *` and
  `Files-Included:` the upstream files the build reads, as `pie-trace`
  found them; the source package keeps nothing else, and its version has a
  `+dsN` suffix. Its other paragraphs cover only those files.
  `debian/pie-leave-out` lists any file the build reads that the source
  package must still leave out (for its licence), and any licence file
  the build never reads that `pie-trace` keeps for its name. A package
  with upstream components (`gbp.conf`'s `component` list) has a
  `Files-Excluded-COMPONENT: *` and `Files-Included-COMPONENT:` pair for
  each, which `tools/pie-components` writes from `pie-trace`'s list.
  lintian does not read those fields, so such a package overrides
  `source-ships-excluded-file` for each component.
- `debian/changelog`, `debian/source/format`
  (`3.0 (quilt)`), and `debian/source/options` with
  `extend-diff-ignore = "^\.github/"`.
- `.github/workflows/build.yml`, as shown above.

For an example, see [blueberrypieos/bash](https://github.com/blueberrypieos/bash),
or [blueberrypieos/xxhash](https://github.com/blueberrypieos/xxhash) for a
library and its `-dev` package.

## Updating pietoolchain

pietoolchain's releases are on blueberrypieos/pietoolchain. A tag is the
version (`0.1.0`), and its CI builds and releases both
`pietoolchain_VERSION_amd64.deb` and `pietoolchain-sdk_VERSION_all.deb`.
To move to a new release, update `PIETOOLCHAIN_VERSION` and the two
SHA-256s in `build.yml` (the release assets'), set `WORKFLOWS_REF` in
`build.yml` to the next version (`v1.1`, `v2`, ...), merge the change, and
tag that commit with that version. A packaging
repository that needs the new toolchain moves to the new tag and raises its
`Build-Depends: pietoolchain (>= VERSION)`. The others can stay on the tag
they use.
