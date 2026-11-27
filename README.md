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
    uses: blueberrypieos/workflows/.github/workflows/build.yml@v4
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
    uses: blueberrypieos/workflows/.github/workflows/attest.yml@v4
    with:
      artifact: ${{ needs.build.outputs.artifact }}
  release:
    needs: [build, attest]
    if: github.event_name == 'push' && github.ref_type == 'tag'
    permissions:
      contents: write
    uses: blueberrypieos/workflows/.github/workflows/release.yml@v4
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
2. checks out pinned submodules recursively with shallow history, while
   retaining the parent repository's full history and tags, then makes the
   source package with `tools/pie-source source`:
   - the orig tarball comes from the upstream tag (`upstream-tag` in
     `debian/gbp.conf`; a `+dsN` version uses the tag of the version
     without the suffix), or from the `pristine-tar` branch when
     `debian/gbp.conf` sets `pristine-tar = True`;
   - it keeps only the files that `debian/copyright`'s `Files-Excluded`
     and `Files-Included` leave in, as `mk-origtargz` does, and each
     component's tarball only those that its `Files-Excluded-COMPONENT`
     and `Files-Included-COMPONENT` leave in;
   - `debian/` comes from the commit being built, and nothing else may
     differ from the upstream tag;
3. checks the source package with `tools/pie-verify check`: lintian's
   copyright tags; `debian/copyright` against every file in the package,
   with no unused pattern or licence; licensecheck through `lrc`; the
   restricted-text markers; and files for other architectures;
4. uploads it as the artifact `SOURCE_VERSION_source`.

The `binary` job runs in a new `ubuntu:26.04` container. It checks out only
this repository's tools, never the packaging repository:

1. checks out this repository's tools into `.workflows`;
2. downloads pietoolchain-host from the pietoolchain-host release it pins,
   and pietoolchain-sdk from the pietoolchain release it pins, checks their
   SHA-256 hashes and installs them;
3. extracts the source package with `dpkg-source -x`;
4. installs the build dependencies (`apt-get build-dep`);
5. builds the binary packages from the extracted source alone:

   ```sh
   dpkg-buildpackage -a darwin-arm64 -b -us -uc -j$(nproc)
   ```

6. merges the source and binary `.changes`, and collects every file it
   lists: the source package (`.dsc`, `.orig.tar.*`, `.debian.tar.*`), the
   `.deb` of each binary package, the `.buildinfo`, and the `.changes`
   itself;
7. uploads them as one artifact, named `SOURCE_VERSION` (the version
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

The reusable build workflow does not run tests. The test workflow checks
this repository on pushes and pull requests.


### Ports that build against other ports

A port whose Build-Depends name other ports' `-dev` packages passes the
ports' apt repository to its `build` job:

```yaml
    uses: blueberrypieos/workflows/.github/workflows/build.yml@v4
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
`Build-Depends: pietoolchain-host (>= 0.3.0), pietoolchain-sdk (>= 0.2.0)`,
for `pie-cross-deps`.

## What a packaging repository contains

The upstream tree is on the upstream branch, `main`, with its release
tags. The `blueberry-pie-os/latest` branch has that tree plus:

- `debian/gbp.conf`, with the DEP-14 branch and tag names
  (`upstream-branch`, `upstream-tag`,
  `debian-branch = blueberry-pie-os/latest`,
  `debian-tag = blueberry-pie-os/%(version)s`).
- `debian/control`, with `Build-Depends: pietoolchain-host (>= VERSION),
  pietoolchain-sdk (>= VERSION)` (plus
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

The host tools are released on blueberrypieos/pietoolchain-host, as
`pietoolchain-host_VERSION_amd64.deb`. The SDK is released on
blueberrypieos/pietoolchain, as `pietoolchain-sdk_VERSION_all.deb`. A tag is
the version (`0.3.0`). To move to a new release, update
`PIETOOLCHAIN_HOST_VERSION` or `PIETOOLCHAIN_SDK_VERSION` and the matching
SHA-256s in `build.yml` (the release assets'), update the same pins in
`tools/root-recipe/recipe.json`, set `WORKFLOWS_REF` in
`build.yml` to the next version (`v1.1`, `v2`, ...), merge the change, and
tag that commit with that version. A packaging
repository that needs the new toolchain moves to the new tag and raises its
`Build-Depends` on pietoolchain-host or pietoolchain-sdk. The others can
stay on the tag they use.

## Proof tools

Run `tools/pie --help` to list the commands. `tools/pie source ARGS` runs
`pie-source ARGS`. The same applies to trace, prove, verify, copyright,
components and clean-matrix.
Add a module with `main()` under `tools/pielib/commands/` to add a command.
An underscore in a module name becomes a hyphen in the command name.

Use `tools/pie prove REPO NAME [CHROOT] [JOBS]`. Omitted, empty or `-`
CHROOT uses the cached recipe-built root. JOBS defaults to 8 and remains
the fourth argument. Use `tools/pie prove REPO NAME - 4` for four jobs
with the default root. A numeric third argument is refused as ambiguous.
An explicit CHROOT keeps using that directory.

Keep the whole `tools/` directory together. The entry points load `pielib`
from beside themselves. The library uses Python's standard library and
python3-debian. It shares the fixed environment, path traversal, DEP-5
matching and copyright merging. Source filters match directories as well as
files.
DEP-5 editing keeps raw paragraphs so licence texts and formatting survive.
Debian control and source metadata use python-debian. The shell entry
points start with bash -p and set their own environment.
Only `pie-clean-matrix-cases` is copied into a chroot. It is standalone.

Run `pytest-3` from this checkout. Tests need python3-pytest and
python3-debian. The push and pull request workflow also checks shell syntax
and shellcheck errors.

Run `pytest-3 tests/test_sdk_parity.py --pietoolchain=/path/to/pietoolchain`
to check the SDK's independent copyright merge against pielib. It
compares matching, merge placement, rendered paragraphs and effective file
coverage across 300 fixed-seed cases.

The clean matrix cases can run on a host with the SDK installed:
`pytest-3 tests/test_clean_matrix.py --clean-matrix-dsc=/path/to/source.dsc`.
The source package must use the SDK's rules.mk and have no other .dsc beside
it. The runner uses a fresh directory under /tmp. Its checks for unsafe
runner paths run without the SDK. The full cases are skipped unless given
a source package. Run the chroot matrix separately.

Each build mounts a fresh tmpfs on /tmp, /var/tmp and /run. Verification
also counts reads under /var/tmp and /run.
Each trace runs in its own process group. Failure and signals terminate
both groups and wait for the traces. Successful traces require a nonempty
set of debs matching the build's .changes names, sizes and SHA-256 values.
Trace metadata records the expected names.
Check-only and resume accept only traces that record private mounts on
/tmp, /var/tmp and /run. Rebuild a trace that doesn't, without check-only
or resume. All pie-source steps use the fixed environment, including
binary builds. DEP-5 escapes work in source filters, trace lists and
allowed removals as they do in copyright files.

### Recipe-built roots

`tools/pie root build` builds or reuses the current recipe's root and prints
its path. `tools/pie-root build` is the same command. `tools/pie root path`
prints an existing completed root without building. `tools/pie root hash`
prints the recipe hash. Build progress goes to standard error.

The recipe is `tools/root-recipe/recipe.json`. It uses the Ubuntu snapshot
at `20261028T020133Z`, exact native package versions, signed PIE sources
and the public keyrings beside the recipe. It includes mount, util-linux,
procps, strace and python3-debian. mmdebstrap first configures the essential
packages, then installs the pinned include list. It registers darwin-arm64
with dpkg. Device nodes are omitted. pie-trace binds the host's /dev before
building. Ubuntu sources select amd64. The PIE ports source selects
darwin-arm64. The installed pie-cross-deps resolves target build
dependencies into its private cross-root during the proof.

The hash covers canonical JSON of the recipe, the public keyring hashes
and the bytes of the root builder and its entry points. Whitespace and JSON
object key order do not affect it. Changing a pin, key or builder changes
the cache key. The cache entry is
`$XDG_CACHE_HOME/pie/roots/HASH`, or `~/.cache/pie/roots/HASH`.
Its unpacked root is `HASH/root`; the recipe and completion records sit
beside it. Set `PIE_ROOT_CACHE` to choose another cache directory.
`PIE_ROOT_RECIPE` chooses another recipe file with its relative keyrings.
The root command also accepts `--cache DIR`, `--recipe FILE` and
`--tmpdir DIR`.

Building needs mmdebstrap with unshare support, util-linux, uidmap, tar and xz,
plus the shared Python dependencies. The user needs subordinate UID and
GID mappings and permission to create user namespaces. On Ubuntu this may
depend on the AppArmor profiles for mmdebstrap and unshare. Install the
tools with `apt install mmdebstrap util-linux uidmap tar xz-utils python3-debian`.

mmdebstrap stages its tarball and keys under `/var/tmp` by default.
`PIE_ROOT_TMPDIR` selects another existing directory whose ancestors are
world-traversable. This stage does not use TMPDIR. An inaccessible setting
fails with a message naming PIE_ROOT_TMPDIR. The finished root is unpacked
through profiled unshare with the invoking user mapped to root.
The cache may be below a private home directory.

The builder checks every pin, dpkg architectures, sources, keys and the
installed recipe record. It builds in staging directories and publishes
the completed entry with a rename. Concurrent builds of one hash take a
lock. Existing entries with missing or mismatched completion records are
refused. The command only cleans staging directories it created and never
repairs, replaces or deletes an unknown root.

With the default root, pie-prove enables unshare and writes
`root: recipe sha256:HASH` in NAME-prove.txt. With an explicit directory it
writes `root: explicit CHROOT`. Each default-root proof copies the cached
root with `cp --archive --reflink=auto` into a unique `.proof-HASH-…`
directory beside the cache entry. Profiled unshare maps the invoking user
to root. Both traces use that working copy. Native dependencies, apt state
and trace work stay in the copy. It is removed on success, failure,
interrupt or termination. Per-run directories are never treated as
completed cache entries. Proofs never write the cached root.

Default-root proofs cannot use check-only or resume. Use an explicit
CHROOT for these modes.

### Comparing packages and trees

Use `tools/pie compare OLD NEW --mode bytes|package|tree [--out DIR] [--json]`.
Exit status is 0 for identical inputs, 1 for differences and 2 for invalid
input, a resource failure or a failed diagnostic tool. `--out` names a
directory. It gets `summary.txt` and the full `report.json`. Standard output
contains the text summary. Use `--json` for the full JSON report instead.
The formats are never mixed. Existing reports and diagnostic log directories
are refused. Other files are kept. Non-UTF-8 deb filenames are an input error.

`bytes` is the acceptance check. Inputs are two debs or directories of debs.
Each input must have at least one deb. The command requires the same set
of `.deb` filenames and the same SHA-256 for every file. It lists missing,
extra and different debs. It ignores no bytes or control fields. Other
files in the directories are outside this check.
Both archives of every deb are validated, including identical and unpaired
debs. The archive reader is shared with pie-verify. It rejects duplicate
member names and hard links without a preceding regular file as their
target. Bad headers and non-zero bytes after the tar end marker are errors.
Both commands use the same SHA-256 file hashing helper. Verification retains
raw member names and uses its original key spelling for allowed removals.

`package` diagnoses differences. Directory inputs are paired by Package
and Architecture, even across versions. More than one deb with the same
identity on either side is an error. Two explicit debs are compared
directly, including differences in Package and Architecture. Unpaired
packages are listed with their complete inventories.

Each package is copied into a fresh private directory before validation.
Only control files are unpacked. Their paths must be relative and contain
no `..` component. Control members must be regular files or the root
directory. Control links are refused. Data inventories are read from the
tar stream without writing files or following links. Absolute and relative
link targets are recorded as metadata. Nothing is skipped. Diagnostics run
only on the validated deb copies. They never receive an unpacked tree.
The report records the original paths and the temporary validated paths.
Temporary copies are removed after the comparison. File hashes are streamed.
An input or resource failure gets status 2 and an error report when `--out`
is given. Errors identify the original deb path.

The report contains every control field and its old and new values when
it differs. It includes the raw control text and both control and data
archive inventories. Each member lists its type, mode, numeric and named
owners, link target, timestamp, device numbers and extended headers. Raw
member names and positions preserve differences in spelling and order.
Regular files and hard links have a size and SHA-256. Other entries have
no file hash. Every member difference is listed, including timestamp and
maintainer script changes. Differences in archive bytes are reported even
when the decoded members match. The ar inventory gives every member's
name, position, size, SHA-256, raw header and padding. It shows extra members
and changes in compression. Filename changes are differences too. The
summary names up to 20 changed members per archive. Complete lists remain
in the report. Any byte difference without an itemised explanation is
explicitly reported.

For each different pair, the full report includes `debdiff` with directory
and all control file comparisons, and `diffoscope --text` when installed.
diffoscope compares the whole deb so it can describe differing files and
archive structure. `--force-details` prevents matching stale md5sums from
hiding file content differences. Its default output limits are disabled.
Tool output, errors and exit status are retained. The summary gives only
the last error line when a diagnostic fails. With `--out`, complete stderr
is also saved in `diagnostics/NNN-TOOL.log`. Full stderr remains in the JSON.
A missing diffoscope is stated in the summary and report. A missing
debdiff or a failed diagnostic is an error.
These tools' own comparison rules never filter the inventories or the
control field report. Package diagnosis does not replace byte acceptance.
The commands need dpkg-deb and debdiff in addition to the shared Python
dependencies. diffoscope is optional.

`tree` shows git tree IDs and `git diff --stat --summary`. Give it two refs or tree
IDs from the current repository, or two directories. Directories are
snapshots of their current contents, including ignored and untracked
files. All `.git` entries and empty directories are omitted. Git records
file contents, executable modes and symlink targets, with no ownership or
timestamp metadata. Special files are rejected. The temporary object
database stays outside the inputs. No index or ref is written in an input
repository. Directory snapshots hash file bytes without git filters in one
`git hash-object --stdin-paths --no-filters` call for both inputs. Mode-only
changes appear in the diff summary.

Examples:

```sh
tools/pie compare full/debs reduced/debs --mode bytes --out compare-bytes
tools/pie compare old.deb new.deb --mode package --out compare-package
tools/pie compare HEAD~ HEAD --mode tree --out compare-tree
tools/pie compare old-tree new-tree --mode tree
tools/pie compare old.deb new.deb --mode package --json
```

### Recording cross dependencies

Pass `--cross-deps` to `pie trace`, or set `PIE_TRACE_ARGS=--cross-deps`
for `pie prove`. The installed `pie-cross-deps` resolves the dependencies.
Each trace retains the actual archives and writes `cross-deps.lock` with
sorted tab-separated fields: side, package, architecture, version and
SHA256 of the deb. Side is `build` for native dependencies.
Side is `host` for unpacked darwin-arm64 dependencies. `base.lock` records
the native packages and versions installed before dependency preparation.
The full build records native packages that dependency preparation adds or
upgrades. The reduced build carries that native selection forward.
It records the actual installed versions, including packages already
present before preparation. It also records new native changes.
Unchanged packages already in the full build's base stay in `base.lock`.

Reference native archives are checked by identity and SHA256.
They are available only during dependency preparation. They do not pin
APT's resolution. Missing archives are fetched by exact installed version.
No replacement versions are resolved. A missing or changed native package
or archive causes comparison to fail.

Use `pie trace --cross-deps --cross-deps-lock FILE` to require an identical
resolved lock. A differing package, version or hash fails with a diff.
With cross dependencies, `pie prove` finishes the full build first and
requires the reduced build's lock to match. Resume and check-only require
matching locks too. Retained trace metadata, logs and dependency files
determine whether locks are required, even if PIE_TRACE_ARGS omits
--cross-deps. Resume then records and checks the new reduced build's
dependencies. Missing required locks fail. Rebuild older traces that lack
them. `base.lock` records each build's starting native environment.

The binary CI job retains the same locks under `.cross-deps/` in its build
artifact. Toolchain release versions and hashes are in `build.yml`.
