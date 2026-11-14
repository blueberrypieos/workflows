# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: pie-trace --chroot ROOT DSC OUT [-j N] [-d] [--cross-deps] [--name NAME] [--leave-out FILE] [--full]
                 [--arch ARCH] [--build b|B|A] [--slot S] [--unshare] [--keep FILE] [--map FROM=TO]... [--scope PREFIX]...
       pie-trace --analyse OUT [--leave-out FILE] [--keep FILE] [--map FROM=TO]... [--scope PREFIX]...

Extracts DSC in ROOT, resets the access times of its files, and builds its
binary packages under strace (run from the host). Then it sorts every file
of the orig tarball(s) into:
  read       opened for reading by the build. Reads of copies made by
             whole-tree copy steps (tar, cp -a) count for the file copied.
  stat       only checked for existence
  untouched  never seen
The build runs in ROOT/work/trace-NAME, or trace-NAME@SLOT with --slot,
which is removed first and may not be reached through a symlink. With
--slot it is mounted at ROOT/work/trace-NAME, which is made and left. NAME
is by default the .dsc's Source. NAME and SLOT have only letters, digits and
._+- and start with a letter or digit. The names of the .dsc and of the files
it lists follow the same rule, with ~ allowed too. --arch has only lower-case
letters, digits and -.
OUT is removed and made anew. It gets meta.json, trace.log, build.log, debs/,
cross-deps.log, files.tsv, outside.txt, kept.txt, left-out-used.txt, files-included.txt,
tree-changes.txt and report.txt. --analyse redoes the sorting. OUT may not
be in ROOT/work, or be or hold ROOT, ROOT/work or the work directory.
Neither OUT nor the work directory may hold the .dsc, a file it lists, the
--leave-out or --keep file, ROOT, or a link on the way to one. ROOT may
not be /, and OUT may not be in ROOT. --analyse refuses an OUT that is a
link, and writes its analysis files anew. Each command pie-trace runs in
ROOT gets an empty /tmp of its own.
-j N sets the build's jobs, by default the number of CPUs. -d passes -d to
dpkg-buildpackage.
The build's PIE_CROSS_ROOT is cross-root in the work directory, made empty.
The chroot's own /var/lib/pietoolchain/cross-root is never used.
--cross-deps fills it as CI does: apt-get update, then pietoolchain's
pie-cross-deps on the extracted source, before the traced build. It
installs the build machine's Build-Depends in ROOT and unpacks the
darwin-arm64 ones from the ports' repository, which must be an apt source
in ROOT. It implies -d. Its output goes to cross-deps.log. One build at a
time runs it in a chroot, under flock on /work/.pie-cross-deps.lock there.
pie-cross-record stages outside ROOT, mounted on a private /run/pie-cross for
dependency resolution only. It retains apt status and dependency debs in
OUT/cross-deps before compilation. Installed target providers need matching cached debs.
pie-trace and what it runs see only PATH, TMPDIR and
PIE_UNSHARE of the caller's environment, with LC_ALL=C.UTF-8 and
HOME=/nonexistent.
The build must leave the extracted tree as it was, apart from what
rules.mk's clean removes: debian/build, debian/PACKAGE, debian/files and
debian/*.substvars. pie-trace compares every path's type, mode, owner,
modification time, link target and contents before and after the build.
Only debian/'s own modification time is left out. It lists any change in
tree-changes.txt and fails.
--unshare (or PIE_UNSHARE=1) runs the build in a user namespace, without sudo,
for a chroot that belongs to the user.
--arch is the architecture to build for (darwin-arm64; host builds for the
machine itself).
--leave-out names files (DEP-5 patterns, one per line, # comments) that the
source package must not keep even though the build reads them. --full marks
DSC as the full upstream tree: the build gets PIE_FULL_SOURCE=1, so that
rules.mk's pie-source-guard lets it through.
--keep names files (DEP-5 patterns, one per line, # comments) the source
package must keep although the build only checks that they exist, where the
trace cannot tell them from files a tool merely looks at (OpenSSL's
Configure checks the sources of every target, built or not).
--map declares a copy the build makes of part of the tree: FROM and TO are
paths in the build (under the extracted source, or absolute in the chroot);
a read of TO/X counts as a read of FROM/X, and copy tools' reads of FROM do
not count. Maps chain. Without a map, a copy tool that reads a file of the
tree and writes a file of the same name makes a copy of it, which counts the
same way. Once the build replaces a file (writes it from the
start, creates it anew, or renames another file onto it), later reads of it
are reads of the build's own file, and a check that it exists made before
then does not count either. The copy that a map declares is not a
replacement: a copy tool writing TO/X, unless that tool read a file of the
same name from outside the mapped sources (libtoolize's cp of its own
ltmain.sh). --scope limits the sorting to the members under the
given prefixes; every other member is kept. What pietoolchain's
pie-source-guard reads does not count: it runs only when the source package
is not the full one. A 3.0 (native) package's one
tarball is its whole source.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pielib.dep5 import regex as dep5
from pielib.environment import fixed_environment
from functools import partial
from pielib.paths import traversed as _traversed
from pielib.deb822 import dsc_fields, dsc_files, control_paragraphs
from pielib.paths import fresh, within
from pielib.proof import PRIVATE_TMPFS


COPY_TOOLS = {"cp", "tar", "rsync", "cpio", "pax", "bsdtar", "find", "ditto"}


NEEDS_EXISTING = {"cmake", "automake", "autoreconf", "make", "configure"}


AUTOCONF_AUX = {"install-sh", "missing", "compile", "depcomp", "config.guess", "config.sub", "ltmain.sh",
                "ar-lib", "test-driver", "mkinstalldirs"}
SHELLS = {"sh", "bash", "dash"}
INTERPRETERS = {"sh", "bash", "dash", "perl", "python3", "python"}


UPDATERS = {"dh_update_autot", "dh_autoreconf", "autoreconf", "libtoolize", "aclocal", "autoconf", "autom4te",
            "automake", "autopoint", "gettextize"}
LICENCE = re.compile(r"^(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|APPLE_LICENSE|APSL)([._-].*)?$", re.I)


def die(msg):
    sys.exit("pie-trace: " + msg)

traversed = partial(_traversed, tool="pie-trace")


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


UNSHARE = bool(os.environ.get("PIE_UNSHARE"))


SUDO_ENV = ["HOME=/root", "LANG=C.UTF-8", "LOGNAME=root", "SHELL=/bin/bash", "TERM=unknown", "USER=root",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"]


def id_map():
    return (["--map-auto"] if shutil.which("newuidmap") else []) + ["--map-root-user"]


def sudo(*cmd, **kw):
    return sh((["unshare"] + id_map() if UNSHARE else ["sudo", "-n"]) + list(cmd), **kw)


def in_root(root, script, wrap=(), bind=None, staging=None):
    variable = "$0" if UNSHARE else "$r"
    temporary = "".join('mount -t tmpfs -o mode=%s tmpfs "%s%s" && ' % (mode, variable, path)
                        for path, mode in PRIVATE_TMPFS.items())
    if not UNSHARE:
        setup = 'r=$1 h=$2 i=$3; shift 3; '
        staged = ''
        if staging:
            setup = 'r=$1 h=$2 i=$3 s=$4; shift 4; '
            staged = 'mkdir -p "$r/run/pie-cross" && mount --bind "$s" "$r/run/pie-cross" && '
        return ["sudo", "-n", "unshare", "--mount", "/bin/sh", "-c",
                setup + temporary +
                '{ [ -z "$h" ] || { mkdir -p "$r$i" && mount --bind "$h" "$r$i"; }; } && ' + staged + 'exec "$@"',
                "sh", root, bind[0] if bind else "", bind[1] if bind else ""] + ([staging] if staging else []) + list(wrap) + ["chroot", root, "/bin/sh", "-c", script]
    proc = "mount -t proc proc /proc && " if wrap else ""
    mnt = 'mkdir -p "$0$3" && mount --bind "$2" "$0$3" && ' if bind else ""

    mnt += temporary
    if staging:
        mnt += 'mkdir -p "$0/run/pie-cross" && mount --bind "$4" "$0/run/pie-cross" && '
    bindings = list(bind) if bind else []
    if staging:
        bindings = (bindings or ["", ""]) + [staging]
    return ["unshare"] + id_map() + ["--mount", "--pid", "--fork", "/bin/sh", "-c",
            proc + mnt + 'mount --rbind /dev "$0/dev" && mount -t proc proc "$0/proc" && exec %s chroot "$0" /bin/sh -c "$1"'
            % shlex.join(["env", "-i"] + SUDO_ENV + list(wrap)), root, script] + bindings


def orig_members(dscdir, files):
    members, debian = {}, []
    for f in files:
        m = re.match(r".*\.orig(?:-([A-Za-z0-9][-A-Za-z0-9]*))?\.tar\.\w+$", f)
        native = not m and re.match(r".*\.tar\.\w+$", f) and not re.match(r".*\.debian\.tar\.\w+$", f)
        if not m and not native:
            continue
        comp = m.group(1) if m else None
        out = subprocess.run(["tar", "--list", "--verbose", "--numeric-owner", "--quoting-style=literal", "-f",
                              os.path.join(dscdir, f)], check=True, capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split(None, 5)
            kind, name = parts[0][0], parts[5]
            if kind in "lh":
                name = name.split(" -> " if kind == "l" else " link to ", 1)[0]
            name = name.split("/", 1)[1] if "/" in name else ""

            if not comp and (name == "debian" or name.startswith("debian/")):
                if m:
                    debian.append(name)
                continue
            if not name or kind == "d":
                continue
            members[(comp + "/" if comp else "") + name] = kind
    return members, debian


def tree_state(src):
    control = sudo("cat", os.path.join(src, "debian/control"), capture_output=True, text=True).stdout
    prune = ["-o", "-regex", r"\./debian/[^/]*\.substvars"]
    for p in ["debian/build", "debian/files"] + \
            ["debian/" + p["Package"] for p in control_paragraphs(control) if "Package" in p]:
        prune += ["-o", "-path", "./" + p]
    find = 'cd "$1" && shift && find . -xdev "(" "$@" ")" -prune -o '
    listing = sudo("sh", "-c", find + '-printf "%y %m %U:%G %T@ %p\\0%l\\0"', "sh", src, *prune[1:],
                   capture_output=True).stdout.decode("utf-8", "surrogateescape").split("\0")
    files = sudo("sh", "-c", find + "-type f -print0", "sh", src, *prune[1:], capture_output=True).stdout
    sums = sudo("sh", "-c", 'cd "$1" && xargs -0 -r sha256sum -z', "sh", src, input=files,
                capture_output=True).stdout.decode("utf-8", "surrogateescape").split("\0")
    state = {}
    for entry, target in zip(listing[0::2], listing[1::2]):
        kind, mode, owner, mtime, path = entry.split(" ", 4)

        state[path] = (kind, mode, owner, "-" if path == "./debian" else mtime, target)
    hashed = set()
    for line in sums:
        if line:
            digest, path = line.split("  ", 1)
            state[path] = state[path][:4] + (digest,)
            hashed.add(path)
    unhashed = sorted(p for p, s in state.items() if s[0] == "f" and p not in hashed)
    if unhashed:
        die("the snapshot of %s has no sha256 for %s" % (src, ", ".join(unhashed[:5])))
    return state


def check_name(what, v):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", v):
        die("%s may have only letters, digits and ._+- and must start with a letter or digit: %r" % (what, v))


def collected_debs(work, debs):
    from debian.deb822 import Changes
    expected = {}
    for name in os.listdir(work):
        if name.endswith(".changes"):
            with open(os.path.join(work, name)) as stream:
                changes = Changes(stream)
            for entry in changes.get("Checksums-Sha256", []):
                filename = entry["name"]
                if filename.endswith(".deb"):
                    if os.path.basename(filename) != filename:
                        die("unsafe deb name in .changes: " + filename)
                    expected[filename] = (int(entry["size"]), entry["sha256"])
    actual = sorted(os.listdir(debs))
    if not expected or sorted(expected) != actual:
        die("collected debs do not match .changes: %s vs %s" % (actual, sorted(expected)))
    for name, (size, digest) in expected.items():
        data = open(os.path.join(debs, name), "rb").read()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            die("collected deb does not match .changes checksum: " + name)
    return sorted(expected)


def run_trace(root, dsc, out, jobs, nodeps, name, full=False, arch="darwin-arm64", build_type="b", slot=None, inputs=(),
              cross_deps=False):
    src, version, files = dsc_fields(dsc)
    name = name or src
    check_name("the trace's name", name)
    for f in files + [os.path.basename(dsc)]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*", f):
            die("the .dsc's name, or a file name it lists, is one pie-trace does not take: %r" % f)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", arch):
        die("--arch may have only lower-case letters, digits and -: %r" % arch)
    dsc_sha256 = hashlib.sha256(open(dsc, "rb").read()).hexdigest()
    inside = "/work/trace-" + name

    w = root + inside + ("@" + slot if slot else "")
    if os.path.realpath(root) == "/":
        die("ROOT may not be /")

    if os.path.realpath(w) != os.path.realpath(root) + inside + ("@" + slot if slot else ""):
        die("the work directory %s is reached through a symlink" % w)

    if slot and os.path.realpath(root + inside) != os.path.realpath(root) + inside:
        die("the mount point %s is reached through a symlink" % (root + inside))

    o, wr, rr = os.path.realpath(out), os.path.realpath(w), os.path.realpath(root)
    for d in (rr, rr + "/work", wr):
        if within(d, o):
            die("OUT %s is or holds %s, which removing OUT would remove" % (out, d))
    if within(o, rr):
        die("OUT %s is in the chroot %s" % (out, rr))

    given, dsc = dsc, os.path.realpath(dsc)
    here = os.path.dirname(dsc)
    for p in [given, root] + [os.path.join(here, f) for f in files] + list(inputs):
        for q in traversed(p):
            for top, what in ((o, "OUT"), (wr, "the work directory")):
                if within(q, top):
                    die("%s holds %s, on the way to %s, and removing %s would remove it" % (top, q, p, what))
    bind = (w, inside) if slot else None
    sudo("rm", "-rf", w)
    sudo("mkdir", "-p", w)
    sudo("cp", dsc, *[os.path.join(here, f) for f in files], w + "/")
    sh(in_root(root, "cd %s && dpkg-source -q -x %s src >/dev/null" % (inside, shlex.quote(os.path.basename(dsc))), bind=bind))
    sudo("mkdir", w + "/cross-root")
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "debs"))
    cross = "PIE_CROSS_ROOT=%s/cross-root " % inside
    with fresh(os.path.join(out, "cross-deps.log")) as log:
        if cross_deps:
            recorder = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'pie-cross-record')
            staging = tempfile.mkdtemp(prefix='.cross-deps-', dir=out)
            try:
                shutil.copyfile(recorder, staging + '/pie-cross-record')
                deps = ("apt-get update -q && cd %s/src && %spython3 /run/pie-cross/pie-cross-record "
                        "/usr/share/pietoolchain/pie-cross-deps . /run/pie-cross/evidence") % (inside, cross)
                rc = subprocess.run(in_root(root, "flock /work/.pie-cross-deps.lock sh -c %s" % shlex.quote(deps),
                                            bind=bind, staging=staging), stdout=log, stderr=subprocess.STDOUT).returncode
                if rc != 0:
                    die("pie-cross-deps failed (rc %d); see %s/cross-deps.log" % (rc, out))
                if not UNSHARE:
                    sudo('chown', '-R', '%d:%d' % (os.getuid(), os.getgid()), staging + '/evidence')
                os.rename(staging + '/evidence', out + '/cross-deps')
            finally:
                sudo('rm', '-rf', staging)
    before = tree_state(w + "/src")
    sudo("find", w + "/src", "-xdev", "-exec", "touch", "-a", "-h", "-d", "@0", "{}", "+")
    build = "cd %s/src && %s%sdpkg-buildpackage %s-%s -us -uc -j%d%s > ../build.log 2>&1" % (
        inside, cross, "PIE_FULL_SOURCE=1 " if full else "", "" if arch == "host" else "-a %s " % shlex.quote(arch),
        build_type, jobs, " -d" if nodeps or cross_deps else "")
    strace = ["strace", "-f", "-qq", "-y", "-Y", "--seccomp-bpf", "-s", "4096",
              "-e", "trace=open,openat,openat2,creat,stat,lstat,newfstatat,statx,access,faccessat,"
                    "faccessat2,readlink,readlinkat,execve,execveat,rename,renameat,renameat2,"
                    "chdir,fchdir,clone,clone3,fork,vfork",
              "-e", "status=successful", "-o", os.path.abspath(os.path.join(out, "trace.log")), "--"]
    rc = subprocess.run(in_root(root, build, strace, bind)).returncode
    sudo("cp", w + "/build.log", out + "/")
    if not UNSHARE:
        sudo("chown", "-R", "%d:%d" % (os.getuid(), os.getgid()), out)
    for f in os.listdir(w):
        if f.endswith(".deb"):
            shutil.copy(os.path.join(w, f), os.path.join(out, "debs"))
    expected_debs = collected_debs(w, os.path.join(out, "debs")) if rc == 0 else []
    atimes = {}
    members, orig_debian = orig_members(here, files)
    for path in members:
        try:
            atimes[path] = os.lstat(os.path.join(w, "src", path)).st_atime > 0
        except FileNotFoundError:
            atimes[path] = None

    after = tree_state(w + "/src")
    changes = ["%s %s" % ("+" if p not in before else "-" if p not in after else "~", p[2:] or ".")
               for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)]
    with fresh(os.path.join(out, "tree-changes.txt")) as f:
        f.write("".join(c + "\n" for c in changes))
    json.dump({"root": root, "inside": inside, "host": w, "dsc": os.path.abspath(dsc),
               "dsc_sha256": dsc_sha256, "src": src, "version": version,
               "rc": rc, "members": members, "orig_debian": orig_debian, "atimes": atimes,
               "tree_changes": len(changes), "expected_debs": expected_debs,
               "private_tmpfs": PRIVATE_TMPFS if rc == 0 else None},
              fresh(os.path.join(out, "meta.json")))
    if rc != 0:
        die("the build failed (rc %d); see %s/build.log" % (rc, out))
    if changes:
        die("the build changed the source tree (%d paths); see %s/tree-changes.txt" % (len(changes), out))


LINE = re.compile(r"^(\d+)<(.*?)> (\w+)\((.*)\) += (-?\d+)(?:<(.*)>)?(?: \(.*\))?$")


def events(log):
    pending = {}
    for line in open(log, errors="replace"):
        line = line.rstrip("\n")
        m = re.match(r"^(\d+)<.*?> .* <unfinished \.\.\.>$", line)
        if m:
            pending[m.group(1)] = line[:-len(" <unfinished ...>")]
            continue
        m = re.match(r"^(\d+)<(.*?)> <\.\.\. \w+ resumed>(.*)$", line)
        if m:
            line = pending.pop(m.group(1), "") + m.group(3)
        m = LINE.match(line)
        if m:
            yield m.groups()


def analyse(out, leave_out=None, maps=(), scope=(), keep=None):
    if os.path.islink(out) or not os.path.isdir(out):
        die("%s is not a directory, or is a link" % out)
    meta = json.load(open(os.path.join(out, "meta.json")))
    root, tree = meta["root"].rstrip("/"), meta["inside"] + "/src"

    host = meta.get("host", root + meta["inside"]) + "/src"
    members = meta["members"]
    copies = []
    for m in maps:
        a, b = m.split("=", 1)
        a = a if a.startswith("/") else tree + "/" + a
        b = b if b.startswith("/") else tree + "/" + b
        copies.append((os.path.normpath(a), os.path.normpath(b)))

    def chroot_path(p):
        p = os.path.normpath(p)
        return p[len(root):] or "/" if p == root or p.startswith(root + "/") else p

    cwd = {}

    def resolve(args, pid=None, nth=0):
        found = list(re.finditer(r'(?:(?:(\w+)<([^>]*)>|(-?\d+|AT_FDCWD)), )?"((?:[^"\\]|\\.)*)"', args))
        if len(found) <= nth:
            return None
        m = found[nth]
        base, name = m.group(2), m.group(4)
        if name.startswith("/"):
            return os.path.normpath(name)
        if base:
            return chroot_path(os.path.join(base, name))
        if m.group(3) is None and pid in cwd:
            return os.path.normpath(os.path.join(cwd[pid], name))
        return None

    reads = collections.defaultdict(set)
    stats = collections.defaultdict(set)
    writes = collections.defaultdict(set)
    pid_reads = collections.defaultdict(set)
    pid_comm = {}
    sources = [a for a, b in copies]
    targets = [b for a, b in copies]
    replaced = set()
    ever_replaced = set()
    outside_names = collections.defaultdict(set)
    exe = {}
    parent = {}
    checks = []
    guard = set()
    copied = collections.defaultdict(dict)
    filecopy = {}

    def script(path, args):
        argv = re.findall(r'"((?:[^"\\]|\\.)*)"', args)[1:]
        if os.path.basename(path) in INTERPRETERS and len(argv) > 1 and not argv[1].startswith("-"):
            return argv[1]
        return path

    def program(path, args):
        return os.path.basename(script(path, args))[:15]

    def updater(pid):
        for _ in range(3):
            if exe.get(pid) in UPDATERS:
                return True
            pid = parent.get(pid)
        return False

    def in_tree(path):
        return path.startswith(tree + "/")

    def mapped(path):
        for _ in range(8):
            if path in replaced:
                return None
            if path in filecopy:
                path = filecopy[path]
                continue
            for a, b in copies:
                if path.startswith(b + "/"):
                    path = a + path[len(b):]
                    break
            else:
                break
        return None if path in replaced else path

    def written(pid, comm, path):

        copy = any(path.startswith(b + "/") for b in targets) or path in filecopy
        if not in_tree(path) or not (copy or path[len(tree) + 1:] in members):
            return
        if comm in COPY_TOOLS and copy and os.path.basename(path) not in outside_names[pid]:
            replaced.discard(path)
            return
        replaced.add(path)
        ever_replaced.add(path)

    for pid, comm, call, args, ret, decoded in events(os.path.join(out, "trace.log")):
        comm = exe.get(pid, comm)
        pid_comm[pid] = comm
        at = re.search(r"AT_FDCWD<([^>]*)>", args)
        if at:
            cwd[pid] = chroot_path(at.group(1))
        if pid in guard:
            if call in ("clone", "clone3", "fork", "vfork") and ret.isdigit():
                guard.add(ret)
            continue
        if call in ("execve", "execveat") and resolve(args, pid) and \
                os.path.basename(script(resolve(args, pid), args)) == "pie-source-guard":
            guard.add(pid)
            source = mapped(resolve(args, pid))
            if source:
                reads[source].add((pid, comm))
            continue
        if call in ("clone", "clone3", "fork", "vfork"):
            if ret.isdigit():
                parent[ret] = pid
                exe.setdefault(ret, comm)
                if pid in cwd:
                    cwd.setdefault(ret, cwd[pid])
            continue
        if call == "chdir":
            path = resolve(args, pid)
            if path:
                cwd[pid] = path
            continue
        if call == "fchdir":
            m = re.match(r"\d+<([^>]*)>", args)
            if m:
                cwd[pid] = chroot_path(m.group(1))
            continue
        if call in ("rename", "renameat", "renameat2"):
            path = resolve(args, pid, 1)
            if path:
                writes[pid].add(path)
                written(pid, comm, path)
            continue
        if call in ("open", "openat", "openat2", "creat"):
            if not decoded:
                continue
            path = chroot_path(decoded)
            if call != "creat" and ("O_RDONLY" in args or "O_RDWR" in args) and "O_DIRECTORY" not in args:
                if comm in COPY_TOOLS:

                    source = mapped(path)
                    if source and in_tree(source) and source[len(tree) + 1:] in members:
                        copied[pid][os.path.basename(path)] = source
                    elif not any(path.startswith(b + "/") for b in targets):
                        outside_names[pid].add(os.path.basename(path))
                if comm in COPY_TOOLS and any(path.startswith(a + "/") for a in sources):
                    continue
                source = mapped(path)
                if source:
                    reads[source].add((pid, comm))
                    if updater(pid):
                        checks.append((source, pid, comm, path))
            if call == "creat" or "O_WRONLY" in args or "O_RDWR" in args:
                writes[pid].add(path)

                name = os.path.basename(path)
                if comm in COPY_TOOLS and name in copied[pid] and in_tree(path) and path[len(tree) + 1:] not in members:
                    filecopy[path] = copied[pid][name]
                if call == "creat" or "O_TRUNC" in args or ("O_CREAT" in args and "O_EXCL" in args):
                    written(pid, comm, path)
        elif call in ("execve", "execveat"):
            path = resolve(args, pid)
            if path:
                exe[pid] = program(path, args)
                source = mapped(path)
                if source:
                    reads[source].add((pid, comm))
        else:
            path = resolve(args, pid)
            if path:
                source = mapped(path)
                if source:
                    stats[source].add((comm, path))

    for source, pid, comm, used in checks:
        if used in ever_replaced or source in ever_replaced:
            reads[source].discard((pid, comm))
    stats = {p: {c for c, used in v if used not in ever_replaced} for p, v in stats.items()}
    stats = {p: v for p, v in stats.items() if v}

    def rel(path):
        return path[len(tree) + 1:] if path.startswith(tree + "/") else None

    for path, who in reads.items():
        r = rel(path)
        if r in members:
            for pid, comm in who:
                pid_reads[pid].add(r)

    regular = sum(1 for k in members.values() if k not in "lh")
    rules_copy = tree + "/debian/build/src/"
    bulk = {pid for pid, rs in pid_reads.items() if pid_comm.get(pid) in COPY_TOOLS and (
        len(rs) >= 50 or (len(rs) >= regular and writes[pid] and all(p.startswith(rules_copy) for p in writes[pid])))}
    by_name = collections.defaultdict(list)
    for m in members:
        by_name[os.path.basename(m)].append(m)
    roots = collections.Counter()
    for pid, paths in writes.items():
        if pid_comm.get(pid) not in COPY_TOOLS:
            continue
        for p in paths:
            for m in by_name.get(os.path.basename(p), ()):
                if p.endswith("/" + m) and not p.startswith(tree + "/" + m):
                    roots[p[:-len(m) - 1]] += 1
    copy_roots = sorted(r for r, n in roots.items() if n >= 50 or (r + "/" == rules_copy and n >= regular))

    state = {}
    via = collections.defaultdict(set)
    for path, who in reads.items():
        r = rel(path)
        real = [(pid, c) for pid, c in who if pid not in bulk]
        if r in members and real:
            via[r].update(c for _, c in real)
        for cr in copy_roots:
            if path.startswith(cr + "/"):
                m = path[len(cr) + 1:]
                if m in members and real:
                    via[m].update("copy:" + c for _, c in real)
    for m in members:
        state[m] = "read" if via.get(m) else "untouched"
    stat_rel = {rel(p) for p in stats} | {rel(p) for p, who in reads.items() if all(pid in bulk for pid, _ in who)}
    for cr in copy_roots:
        for p in stats:
            if p.startswith(cr + "/"):
                stat_rel.add(p[len(cr) + 1:])
    for m in members:
        if state[m] == "untouched" and m in stat_rel:
            state[m] = "stat"
    for p, comms in stats.items():
        r = rel(p)
        for cr in copy_roots:
            if p.startswith(cr + "/"):
                r = p[len(cr) + 1:]
        if r in members and state[r] == "stat" and comms & NEEDS_EXISTING:
            state[r] = "read"
            via[r].update("stat:" + c for c in comms & NEEDS_EXISTING)
        elif r in members and state[r] == "stat" and os.path.basename(r) in AUTOCONF_AUX and comms & SHELLS:
            state[r] = "read"
            via[r].add("stat:autoconf-aux")

    kept = {m for m, s in state.items() if s == "read"}
    if scope:
        kept |= {m for m in members if not any(m == p or m.startswith(p.rstrip("/") + "/") for p in scope)}
    if keep:
        pats = [dep5(l.split("#", 1)[0].strip()) for l in open(keep) if l.split("#", 1)[0].strip()]
        kept |= {m for m in members if any(p.match(m) for p in pats)}
    patched = set()
    debian = os.path.join(host, "debian", "patches", "series")
    if os.path.exists(debian):
        for line in open(debian):
            p = line.split("#", 1)[0].split()
            if p:
                text = open(os.path.join(os.path.dirname(debian), p[0]), errors="replace").read()
                for mm in re.finditer(r"^\+\+\+ \S+?/(\S+)", text, re.M):
                    if mm.group(1) in members:
                        patched.add(mm.group(1))
    kept |= patched
    for m, kind in members.items():
        if kind == "l":
            target = os.path.normpath(os.path.join(os.path.dirname(m), os.readlink(host + "/" + m))) \
                if os.path.islink(host + "/" + m) else None
            if target in kept:
                kept.add(m)
    dirs = {os.path.dirname(k) for k in kept}
    for m in members:
        if LICENCE.match(os.path.basename(m)):
            d = os.path.dirname(m)
            if any(x == d or x.startswith(d + "/") or d == "" for x in dirs):
                kept.add(m)

    left_used = []
    if leave_out:
        pats = [dep5(l.split("#", 1)[0].strip()) for l in open(leave_out) if l.split("#", 1)[0].strip()]
        for m in sorted(kept):
            if any(p.match(m) for p in pats):
                kept.discard(m)
                if state[m] == "read":
                    left_used.append(m)
    atimes = meta["atimes"]

    atime_only = [] if bulk else sorted(m for m in members if atimes.get(m) and m not in kept)
    outside = sorted(p for p, who in reads.items() if not p.startswith(tree + "/") and not p.startswith(
        ("/proc/", "/dev/", "/sys/", "pipe:", "socket:", "anon_inode:")) and not any(p.startswith(cr + "/") for cr in copy_roots))

    with fresh(os.path.join(out, "files.tsv")) as f:
        for m in sorted(members):
            f.write("%s\t%s\t%s\t%s\n" % ("kept" if m in kept else "out", state[m], ",".join(sorted(via.get(m, ()))), m))
    fresh(os.path.join(out, "kept.txt")).write("".join(k + "\n" for k in sorted(kept)))
    fresh(os.path.join(out, "outside.txt")).write("".join(p + "\n" for p in outside))
    patterns = compress(set(members), kept)
    fresh(os.path.join(out, "files-included.txt")).write("".join(p + "\n" for p in patterns))
    counts = collections.Counter(state.values())
    report = [
        "source: %s %s (build rc %d)" % (meta["src"], meta["version"], meta["rc"]),
        "orig files: %d; kept %d; left out %d" % (len(members), len(kept), len(members) - len(kept)),
        "read %d, existence-checked only %d, untouched %d" % (counts["read"], counts["stat"], counts["untouched"]),
        "patched by debian/patches: %d (%d of them never read)" % (len(patched), len(patched - {m for m in members if state[m] == 'read'})),
        "whole-tree copy steps: %d process(es); copy roots: %s" % (len(bulk), ", ".join(copy_roots) or "none"),
        "orig files the build replaced with its own: %d" % len(
            {p for p in ever_replaced if rel(p) in members or p in filecopy or any(p.startswith(b + "/") for b in targets)}),
        "atime says read but the trace does not (kept out): %s" % (
            "not checked: a whole-tree copy reads every file" if bulk else len(atime_only)),
        "Files-Included patterns: %d" % len(patterns),
        "left out although the build reads them (--leave-out): %d" % len(left_used),
    ]
    if atime_only:
        report += ["  " + m for m in atime_only[:50]]
    fresh(os.path.join(out, "left-out-used.txt")).write("".join(m + "\n" for m in left_used))
    fresh(os.path.join(out, "report.txt")).write("\n".join(report) + "\n")
    print("\n".join(report))


def compress(members, kept):
    children = collections.defaultdict(set)
    for m in members:
        parts = m.split("/")
        for i in range(1, len(parts)):
            children["/".join(parts[:i - 1])].add("/".join(parts[:i]))
        children["/".join(parts[:-1])].add(m)
    full = {}

    def is_full(d):
        if d not in full:
            full[d] = all((c in kept) if c in members else is_full(c) for c in children[d])
        return full[d]

    out = []

    def walk(d):
        for c in sorted(children[d]):
            if c in members:
                if c in kept:
                    out.append(c)
            elif is_full(c):
                out.append(c + "/*")
            elif any(k.startswith(c + "/") for k in kept):
                walk(c)
    if is_full(""):
        return ["*"]
    walk("")
    return [p.replace(" ", "?") for p in out]


def main():
    ap = argparse.ArgumentParser(usage=__doc__[len("usage: "):])
    ap.add_argument("--chroot")
    ap.add_argument("--analyse", action="store_true")
    ap.add_argument("dsc", nargs="?")
    ap.add_argument("out")
    ap.add_argument("-j", dest="jobs", type=int, default=os.cpu_count())
    ap.add_argument("-d", dest="nodeps", action="store_true")
    ap.add_argument("--cross-deps", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--leave-out")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--arch", default="darwin-arm64")
    ap.add_argument("--slot", help="build in a directory of its own, so that other slots can build at once")
    ap.add_argument("--build", choices=("b", "B", "A"), default="b",
                    help="dpkg-buildpackage's -b (all), -B (arch only) or -A (indep only)")
    ap.add_argument("--unshare", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--map", action="append", default=[])
    ap.add_argument("--scope", action="append", default=[])
    a = ap.parse_args()
    fixed_environment(("PIE_UNSHARE",))
    out = os.path.abspath(a.out)
    if not a.analyse:
        if a.unshare:
            global UNSHARE
            UNSHARE = True
        if not (a.chroot and a.dsc):
            die("need --chroot ROOT DSC OUT")
        for what, v in ("--name", a.name), ("--slot", a.slot):
            if v is not None:
                check_name(what, v)
        run_trace(a.chroot, a.dsc, out, a.jobs, a.nodeps, a.name, a.full, a.arch, a.build, a.slot,
                  [p for p in (a.leave_out, a.keep) if p], a.cross_deps)
    analyse(out, a.leave_out and os.path.abspath(a.leave_out), a.map, a.scope, a.keep and os.path.abspath(a.keep))
