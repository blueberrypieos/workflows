# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: pie-source orig [--full] [-C REPO] [-o OUT]
       pie-source source [--full] [-C REPO] [-o OUT]
       pie-source binary DSC [-o OUT] [--work DIR] [-j N] [-d] [--no-network] [-a ARCH]

orig    Makes the upstream tarball for debian/changelog's version from the
        upstream tag (gbp.conf's upstream-tag; X+dsN uses X's tag). It keeps
        only the files that debian/copyright's Files-Excluded and
        Files-Included leave in, as mk-origtargz does. Each component in
        gbp.conf gets its own tarball from its directory of the upstream
        tree, filtered by Files-Excluded-COMPONENT and
        Files-Included-COMPONENT.
source  Makes the source package from that tarball and HEAD's debian/. For
        a 3.0 (native) package, the tree is HEAD plus each submodule at the
        commit HEAD records, less what Files-Excluded and Files-Included
        leave out. When gbp.conf's upstream-tree is SLOPPY, that tree
        without debian/ is the upstream tarball.
binary  Builds the binary packages from DSC alone, in a new directory, and
        merges the source and binary .changes. The directory is --work, by
        default OUT/.binary, and is removed first if it is there.
--full  Keeps every upstream file. Only for the analysis build.
REPO is by default the working directory, and OUT ../build-area from the
working directory, not from REPO. N is by default the number of CPUs, and
ARCH darwin-arm64.
orig, source and binary write into OUT, each file through a new file that
replaces any file or link of its name.
They run their tools with only PATH, TMPDIR and PIE_QUIET from the
environment, with LC_ALL=C.UTF-8 and HOME=/nonexistent. git takes no
replace refs. They stop if the repo's git config gives a filter driver a
smudge or process command, or its info/attributes has rules. Every git
command gets safe.directory for the repo, no fsmonitor and no gpg program.
They read debian/ only from HEAD, never from the working tree.
binary refuses a --work that is / or holds DSC, a file it lists, or a link
on the way to one.
"""
import argparse
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pielib.dep5 import dep5_matcher
from pielib.environment import fixed_environment
from functools import partial
from pielib.paths import traversed as _traversed
from pielib.deb822 import dsc_fields, dsc_files, control_paragraphs


os.umask(0o022)


def git(where):
    return ["git", "-c", "safe.directory=" + os.path.realpath(where), "-c", "core.fsmonitor=false",
            "-c", "gpg.program=false", "-c", "gpg.ssh.program=false", "-c", "gpg.x509.program=false"]


def clean_environ():
    fixed_environment(("PIE_QUIET",))
    os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"


def die(msg):
    sys.exit("pie-source: " + msg)

traversed = partial(_traversed, tool="pie-source")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


_gbp_conf = []
_head_debian = []


def head_debian(repo):
    if not _head_debian:
        d = tempfile.mkdtemp(prefix="pie-source-debian.")
        atexit.register(shutil.rmtree, d, True)
        cmd, env = archive(repo, ["HEAD", "debian"])
        proc = subprocess.Popen(cmd, cwd=repo, stdout=subprocess.PIPE, env=env)
        run(["tar", "-xf", "-", "-C", d], stdin=proc.stdout)
        if proc.wait() != 0:
            die("git archive HEAD debian failed")
        _head_debian.append(os.path.join(d, "debian"))
    return _head_debian[0]


def gbp_env(repo):
    if not _gbp_conf:
        fd, path = tempfile.mkstemp(prefix="pie-source-gbp.", suffix=".conf")
        atexit.register(os.unlink, path)
        with os.fdopen(fd, "w") as f:
            if output(git(repo) + ["ls-tree", "--name-only", "HEAD", "--", "debian/gbp.conf"], cwd=repo):
                run(git(repo) + ["show", "HEAD:debian/gbp.conf"], cwd=repo, stdout=f)
        _gbp_conf.append(path)
    return dict(os.environ, GBP_CONF_FILES=_gbp_conf[0])


def archive(where, args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_NO_REPLACE_OBJECTS="1")
    info = os.path.join(where, output(git(where) + ["rev-parse", "--git-path", "info/attributes"], cwd=where, env=env))
    if os.path.exists(info) and any(l.strip() and not l.lstrip().startswith("#") for l in open(info)):
        die("%s has rules, which git archive would apply" % info)
    drivers = output(git(where) + ["config", "--get-regexp", r"^filter\..*\.(smudge|process)$"], cwd=where, env=env,
                     ok=(0, 1)).split()
    if drivers:
        die("the git config of %s gives %s, a command git archive would run" % (where, drivers[0]))
    cmd = git(where) + ["-c", "tar.umask=0022", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                 "-c", "core.attributesFile=/dev/null", "archive", "--format=tar"] + args
    return cmd, env


def publish(src, out, name=None):
    fd, tmp = tempfile.mkstemp(prefix=".pie-source-", dir=out)
    os.close(fd)
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(out, name or os.path.basename(src)))


def source_env():
    home = tempfile.mkdtemp(prefix="pie-source-home.")
    atexit.register(shutil.rmtree, home, True)
    return dict(os.environ, HOME=home, XDG_CONFIG_HOME=home)


def output(cmd, ok=(0,), **kw):
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if res.returncode not in ok:
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res.stdout.strip()


def changelog(debian):
    f = os.path.join(debian, "changelog")
    return output(["dpkg-parsechangelog", "-l", f, "-SSource"]), output(["dpkg-parsechangelog", "-l", f, "-SVersion"])


def upstream_version(version):
    v = version.split(":", 1)[-1]
    return v.rsplit("-", 1)[0] if "-" in v else v


def header(repo):
    text = open(os.path.join(head_debian(repo), "copyright")).read()
    first = control_paragraphs(text)[0]
    return {name: value.split() for name, value in first.items()}


def upstream_commit(repo, base):
    fmt = output(["gbp", "config", "buildpackage.upstream-tag"], cwd=repo, env=gbp_env(repo))
    from gbp.deb.git import DebianGitRepository
    tag = DebianGitRepository.version_to_tag(fmt, base)
    return tag, output(git(repo) + ["rev-parse", "--verify", "--quiet", tag + "^{commit}"], cwd=repo)


def components(repo):
    res = subprocess.run(["gbp", "config", "buildpackage.component"], cwd=repo, capture_output=True, text=True, env=gbp_env(repo))
    value = res.stdout.strip()
    if res.returncode != 0 or not value or value == "[]":
        return []
    import ast
    return list(ast.literal_eval(value))


def make_orig(repo, out, full):
    src, version = changelog(head_debian(repo))
    upver = upstream_version(version)
    base = re.sub(r"\+ds\d*$", "", upver)
    tag, commit = upstream_commit(repo, base)
    fields = header(repo)
    filtered = not full and "Files-Excluded" in fields
    if not full:
        if upver != base and not filtered:
            die("%s is a +ds version, but debian/copyright has no Files-Excluded" % upver)
        if filtered and upver == base:
            die("debian/copyright leaves files out, so the version needs a +dsN suffix")
    os.makedirs(out, exist_ok=True)
    comps = components(repo)
    origs = []
    for comp in [None] + comps:
        name = "%s_%s.orig%s.tar.xz" % (src, upver, "-" + comp if comp else "")
        with tempfile.TemporaryDirectory() as tmp:
            tar = os.path.join(tmp, name[:-3])
            if comp:

                ct = output(git(repo) + ["log", "-1", "--format=%ct", commit], cwd=repo).strip()
                tree, prefix, paths = "%s:%s" % (commit, comp), comp, []
                opts = ["--mtime=@" + ct]
            else:
                tree, prefix, paths = commit, "%s-%s" % (src, upver), ["--", "."] + [":(exclude)%s" % c for c in comps]
                opts = []
            cmd, env = archive(repo, ["--prefix=%s/" % prefix] + opts + [tree] + paths)
            with open(tar, "wb") as f:
                run(cmd, cwd=repo, stdout=f, env=env)
            stanza = "Files-Excluded-" + comp if comp else "Files-Excluded"
            if filtered and stanza in fields:
                res = subprocess.run(["mk-origtargz", "--package", src, "--version", upver, "--compression", "xz",
                                      "--copyright-file", os.path.join(head_debian(repo), "copyright"),
                                      "--directory", tmp, "--copy"] + (["--component", comp] if comp else []) + [tar],
                                     cwd=os.path.dirname(head_debian(repo)), capture_output=True, text=True)
                log = res.stdout + res.stderr
                if res.returncode != 0 or re.search(r"warn|No files matched", log, re.I):
                    sys.stderr.write(log)
                    die("mk-origtargz failed or warned for %s" % name)
                made = os.path.join(tmp, name)
            else:
                if filtered and comp:
                    die("debian/copyright leaves files out, but has no %s field" % stanza)
                made = tar + ".xz"
                with open(made, "wb") as f:
                    run(["xz", "-6", "-T1", "-c", tar], stdout=f)
            publish(made, out, name)
        origs.append((comp, os.path.join(out, name)))
    return src, version, upver, tag, commit, origs


def tar_names(tarball):
    names = output(["tar", "-tf", tarball]).split("\n")
    return {n.split("/", 1)[1] for n in names if "/" in n and not n.endswith("/")}


def created_by_patches(tree):
    series = os.path.join(tree, "debian", "patches", "series")
    made = []
    if not os.path.exists(series):
        return made
    for line in open(series):
        p = line.split("#", 1)[0].split()
        if not p:
            continue
        text = open(os.path.join(tree, "debian", "patches", p[0]), errors="replace").read()
        for m in re.finditer(r"^--- /dev/null[^\n]*\n\+\+\+ (\S+)", text, re.M):
            made.append((p[0], m.group(1).split("/", 1)[1] if "/" in m.group(1) else m.group(1)))
    return made


def gitlinks(repo, commit, prefix=""):
    out = []
    for line in output(git(repo) + ["ls-tree", "-r", "-z", commit], cwd=repo).split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "commit":
            continue
        sub = os.path.join(repo, path)
        if not os.path.exists(os.path.join(sub, ".git")):
            sys.stderr.write("pie-source: %s%s is not checked out; left out\n" % (prefix, path))
            continue
        out.append((prefix + path, sha, sub))
        out += gitlinks(sub, sha, prefix + path + "/")
    return out


def assemble(repo, tree, full):
    os.makedirs(tree)
    for path, commit, where in [("", "HEAD", repo)] + gitlinks(repo, "HEAD"):
        cmd, env = archive(where, (["--prefix=%s/" % path] if path else []) + [commit])
        proc = subprocess.Popen(cmd, cwd=where, stdout=subprocess.PIPE, env=env)
        run(["tar", "-xf", "-", "-C", tree], stdin=proc.stdout)
        if proc.wait() != 0:
            die("git archive failed in %s" % where)
    fields = header(repo)
    if not full and "Files-Excluded" in fields:
        excluded = dep5_matcher(fields["Files-Excluded"])
        included = dep5_matcher(fields.get("Files-Included", []))
        removed = 0
        for d, dirs, files in os.walk(tree, topdown=False):
            rel = os.path.relpath(d, tree)
            if rel == "debian" or rel.startswith("debian/"):
                continue
            for f in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
                path = os.path.normpath(os.path.join(rel, f))
                if excluded(path) and not included(path):
                    os.unlink(os.path.join(d, f))
                    removed += 1
            if rel != "." and not os.listdir(d):
                os.rmdir(d)
        sys.stderr.write("pie-source: left out %d files\n" % removed)


def finish(src, version, tree, work, out):
    run(["dpkg-buildpackage", "-S", "-sa", "-nc", "-d", "-us", "-uc"], cwd=tree, env=source_env(),
        stdout=subprocess.DEVNULL if os.environ.get("PIE_QUIET") else None)
    for f in os.listdir(work):
        if f.startswith(src + "_") and os.path.isfile(os.path.join(work, f)):
            os.replace(os.path.join(work, f), os.path.join(out, f))
    dsc = os.path.join(out, "%s_%s.dsc" % (src, version.split(":", 1)[-1]))
    print(dsc)
    return dsc


def make_native_source(repo, out, full):
    src, version = changelog(head_debian(repo))
    work = tempfile.mkdtemp(prefix=".pie-source-", dir=out)
    try:
        tree = os.path.join(work, "%s-%s" % (src, version))
        assemble(repo, tree, full)
        return finish(src, version, tree, work, out)
    finally:
        shutil.rmtree(work)


def make_sloppy_source(repo, out, full):
    src, version = changelog(head_debian(repo))
    upver = upstream_version(version)
    work = tempfile.mkdtemp(prefix=".pie-source-", dir=out)
    try:
        tree = os.path.join(work, "%s-%s" % (src, upver))
        assemble(repo, tree, full)
        debian = os.path.join(work, "debian")
        shutil.move(os.path.join(tree, "debian"), debian)
        when = output(git(repo) + ["log", "-1", "--format=%ct", "HEAD"], cwd=repo)
        orig = os.path.join(work, "%s_%s.orig.tar.xz" % (src, upver))
        tar = subprocess.Popen(["tar", "--sort=name", "--mtime=@" + when, "--owner=0", "--group=0",
                                "--numeric-owner", "--format=gnu", "-cf", "-", "%s-%s" % (src, upver)],
                               cwd=work, stdout=subprocess.PIPE)
        with open(orig, "wb") as f:
            run(["xz", "-6", "-T1", "-c"], stdin=tar.stdout, stdout=f)
        if tar.wait() != 0:
            die("tar of the upstream tree failed")
        shutil.move(debian, os.path.join(tree, "debian"))
        publish(orig, out)
        return finish(src, version, tree, work, out)
    finally:
        shutil.rmtree(work)


def make_source(repo, out, full):
    if open(os.path.join(head_debian(repo), "source", "format")).read().strip() == "3.0 (native)":
        os.makedirs(out, exist_ok=True)
        return make_native_source(repo, out, full)
    if subprocess.run(["gbp", "config", "buildpackage.upstream-tree"], cwd=repo, env=gbp_env(repo), capture_output=True,
                      text=True).stdout.strip().upper() == "SLOPPY":
        os.makedirs(out, exist_ok=True)
        return make_sloppy_source(repo, out, full)
    src, version, upver, tag, commit, origs = make_orig(repo, out, full)
    if subprocess.run(git(repo) + ["diff", "--quiet", commit, "HEAD", "--", ".", ":(exclude)debian",
                       ":(exclude).github", ":(exclude).gitignore"], cwd=repo).returncode != 0:
        die("HEAD changes upstream files outside debian/: put those changes in debian/patches")
    work = tempfile.mkdtemp(prefix=".pie-source-", dir=out)
    try:
        tree = os.path.join(work, "%s-%s" % (src, upver))
        kept = set()
        for comp, orig in origs:
            shutil.copy(orig, work)
            dest = os.path.join(tree, comp) if comp else work
            os.makedirs(dest, exist_ok=True)
            run(["tar", "-xf", orig, "-C", dest] + (["--strip-components=1"] if comp else []))
            kept |= {(comp + "/" + n if comp else n) for n in tar_names(orig)}
        os.makedirs(tree, exist_ok=True)

        shutil.rmtree(os.path.join(tree, "debian"), ignore_errors=True)
        cmd, env = archive(repo, ["HEAD", "debian"])
        proc = subprocess.Popen(cmd, cwd=repo, stdout=subprocess.PIPE, env=env)
        run(["tar", "-xf", "-", "-C", tree], stdin=proc.stdout)
        if proc.wait() != 0:
            die("git archive HEAD debian failed")
        upstream = set(output(git(repo) + ["ls-tree", "-r", "--name-only", "-z", commit], cwd=repo).split("\0"))
        for patch, path in created_by_patches(tree):
            if path in upstream and path not in kept:
                die("%s creates %s, which the source package leaves out" % (patch, path))
        run(["dpkg-buildpackage", "-S", "-sa", "-nc", "-d", "-us", "-uc",
             "--source-option=--abort-on-upstream-changes"], cwd=tree, env=source_env(),
            stdout=subprocess.DEVNULL if os.environ.get("PIE_QUIET") else None)
        for f in os.listdir(work):
            if f.startswith(src + "_") and os.path.isfile(os.path.join(work, f)):
                os.replace(os.path.join(work, f), os.path.join(out, f))
    finally:
        shutil.rmtree(work)
    dsc = os.path.join(out, "%s_%s.dsc" % (src, version.split(":", 1)[-1]))
    print(dsc)
    return dsc


def build_binary(dsc, out, work, jobs, nodeps, nonet, arch):
    dsc = os.path.abspath(dsc)
    src, version, files = dsc_fields(dsc)
    version = version.split(":", 1)[-1]
    w = os.path.realpath(work)
    for f in [dsc] + [os.path.join(os.path.dirname(dsc), f) for f in dsc_files(dsc)]:
        for q in traversed(f):
            if q == w or w == "/" or q.startswith(w.rstrip("/") + "/"):
                die("--work %s holds %s, which removing it would remove" % (work, q))
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    tree = os.path.join(work, "src")
    run(["dpkg-source", "-q", "-x", dsc, tree], stdout=subprocess.DEVNULL)
    if os.path.exists(os.path.join(tree, ".git")):
        die("the extracted source has .git")
    cmd = ["dpkg-buildpackage", "-a", arch, "-b", "-us", "-uc", "-j%d" % jobs] + (["-d"] if nodeps else [])
    if nonet:
        cmd = ["unshare", "-n", "--"] + cmd
    run(cmd, cwd=tree)
    os.makedirs(out, exist_ok=True)
    here = os.path.dirname(dsc)
    for f in [os.path.basename(dsc)] + dsc_files(dsc):
        if os.path.abspath(here) != os.path.abspath(out):
            publish(os.path.join(here, f), out)
    for f in os.listdir(work):
        if f.endswith((".deb", ".udeb", ".buildinfo", ".changes")):
            publish(os.path.join(work, f), out)
    binary = "%s_%s_%s.changes" % (src, version, arch)
    source = os.path.join(here, "%s_%s_source.changes" % (src, version))
    if os.path.exists(source):
        for f in (source, os.path.join(here, "%s_%s_source.buildinfo" % (src, version))):
            if os.path.exists(f) and os.path.abspath(here) != os.path.abspath(out):
                publish(f, out)
        run(["mergechanges", "-f", os.path.basename(source), binary], cwd=out)
        os.replace(os.path.join(out, "%s_%s_multi.changes" % (src, version)), os.path.join(out, binary))
    print(os.path.join(out, binary))


def main():
    ap = argparse.ArgumentParser(usage=__doc__[len("usage: "):])
    ap.add_argument("step", choices=["orig", "source", "binary"])
    ap.add_argument("dsc", nargs="?")
    ap.add_argument("-C", dest="repo", default=".")
    ap.add_argument("-o", dest="out", default="../build-area")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--work")
    ap.add_argument("-j", dest="jobs", type=int, default=os.cpu_count())
    ap.add_argument("-d", dest="nodeps", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("-a", dest="arch", default="darwin-arm64")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    clean_environ()
    if a.step == "orig":
        for comp, orig in make_orig(os.path.abspath(a.repo), out, a.full)[-1]:
            print(orig)
    elif a.step == "source":
        make_source(os.path.abspath(a.repo), out, a.full)
    else:
        if not a.dsc:
            die("binary needs a .dsc")
        build_binary(a.dsc, out, os.path.abspath(a.work or os.path.join(out, ".binary")), a.jobs, a.nodeps,
                     a.no_network, a.arch)
