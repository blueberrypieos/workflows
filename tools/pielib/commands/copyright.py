# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: pie-copyright prune COPYRIGHT FILES-INCLUDED KEPT DEBIAN-FILES [-o OUT] [--tree TREE] [--component]
       pie-copyright merge COPYRIGHT TREE [-o OUT]
       pie-copyright check COPYRIGHT TREE

prune  Rewrites a DEP-5 copyright for the files a source package keeps:
       the header gets 'Files-Excluded: *' and Files-Included (from
       FILES-INCLUDED), each Files paragraph keeps only the patterns that
       are the last match for some kept file (KEPT holds orig paths,
       DEBIAN-FILES the debian/ paths), paragraphs left with none go, and
       so do licence paragraphs nothing names.
merge  Joins Files paragraphs with the same other fields, at the place of
       either one, when every file in TREE keeps the same fields. It
       repeats until nothing joins. A joined Files field lists its patterns
       sorted, without those that match files but are no file's last
       match. TREE is an extracted source package, as for check.
check  Fails if a file in TREE (an extracted source package) has no Files
       paragraph, if a Files pattern applies to no file or has a bad
       escape, or if a licence paragraph is unused or missing. The files
       TREE/debian/pie-generated lists (built files the binary packages
       contain) count as files. It also fails if merge would join two
       paragraphs.
A licence with an exception, such as "GPL-2.0-or-later with X exception",
is a name of its own and needs its own licence paragraph.
"""
import os
import re
import sys
from pielib.dep5 import regex, bad_escape
from pielib.dep5 import paragraphs, field, licence_names, last_matches, set_field
from pielib.copyright import rest_of, joins, covered, join_once


def die(msg):
    sys.exit("pie-copyright: " + msg)


def fold(text):
    import unicodedata
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\b[A-Z]\.(?=\s|$)", " ", text)
    text = text.replace("ø", "o").replace("æ", "ae").replace("ß", "ss").lower()
    text = re.sub(r"[-_`'\u2019]", " ", text)
    return re.sub(r"\s+", " ", text)


def variants(name):
    import unicodedata
    out = {fold(name)}
    oe = name.replace("ö", "oe").replace("ü", "ue").replace("ä", "ae").replace("Ö", "Oe").replace("Ü", "Ue")
    out.add(fold(oe))
    short = re.sub(r"\b(computer|inc|incorporated|corporation|corp|ltd|limited|llc|gmbh|co)\b\.?", " ", fold(name))
    short = re.sub(r"\s+", " ", short).strip(" ,.")
    if len(re.findall(r"[a-z]", short)) >= 4:
        out.add(short)
    return {v.strip(" ,.") for v in out if v.strip(" ,.")}


def holder_parts(line):
    name = re.sub(r"\([^)]*\)", " ", line)
    name = re.sub(r"\b(copyright|\(c\)|©)\b", " ", name, flags=re.I)
    name = re.sub(r"\b\d{4}(?:\s*[-–]\s*\d{2,4})?\b,?", " ", name)
    name = re.sub(r"<[^>]*>", " ", name).strip(" ,.;")
    parts = [p.strip(" .,;") for p in re.split(r",|\band\b|&", name)]
    parts = [p for p in parts if len(re.findall(r"[^\W\d_]", p)) >= 3 and p.lower() not in ("inc", "ltd", "llc", "others")]
    return name, parts


def holder_entries(lines):
    entries = []
    for l in lines:
        if entries and (re.search(r"\b(and|of|from)$|,$", entries[-1]) or entries[-1].count("(") > entries[-1].count(")")):
            entries[-1] += "\n " + l
        else:
            entries.append(l)
    return entries


def prune_holders(par, files, tree):
    c = field(par, "Copyright")
    if c is None or tree is None:
        return par
    texts = []
    lines = []
    for f in files:
        try:
            data = open(os.path.join(tree, f), "rb").read()
        except OSError:
            continue
        if b"\0" not in data[:8192]:
            texts.append(fold(data.decode("utf-8", errors="replace")))
            texts.append(fold(data.decode("latin-1")))
            lines += [(f, l) for l in data.decode("utf-8", errors="replace").splitlines() if re.search(r"copyright|\(c\)|©", l, re.I)]
    if not texts:
        return par
    blob = "\n".join(texts)
    keep, dropped = [], []
    for e in holder_entries([l.strip() for l in c.split("\n") if l.strip()]):
        name, parts = holder_parts(re.sub(r"\s+", " ", e))
        if not parts or e.lower().startswith(("none", "no copyright", "(")):
            keep.append(e)
        elif any(v in blob for p in parts for v in variants(p)):
            keep.append(e)
        else:
            dropped.append(e)
    if not keep:
        return par
    common = {"university", "regents", "california", "foundation", "computer", "software", "systems",
              "corporation", "international", "institute", "technology", "project", "contributors", "authors"}
    for d in dropped:
        words = {w for w in re.findall(r"[a-z]{5,}", fold(holder_parts(d)[0])) if w not in common}
        seen = sorted(w for w in words if w in blob)
        ev = [("%s: %s" % (f, l.strip()))[:160] for f, l in lines if any(w in fold(l) for w in seen)][:2]
        print("pie-copyright: dropped holder %r from %s%s" % (re.sub(r"\s+", " ", d), (field(par, "Files") or "").split()[:3],
              "  CHECK: %s appear(s) in its files; copyright lines: %s" % (", ".join(seen), " || ".join(ev) or "none") if seen else ""))
    body = "Copyright: " + "\n ".join(keep)
    return re.sub(r"^Copyright:[ \t]*.*(?:\n[ \t]+.*)*", lambda m: body, par, count=1, flags=re.M)


OWN = ("userland/", "xnu-patches/", "debian/")


def prune(copyright, included, kept, debian, out, tree=None, component=False):
    text = open(copyright).read()
    pars = paragraphs(text)
    head, rest = pars[0], pars[1:]
    patterns = [l.strip() for l in open(included) if l.strip()]
    if component:
        pass
    elif patterns == ["*"]:
        head = re.sub(r"^Files-(Excluded|Included):[ \t]*.*(?:\n[ \t]+.*)*\n?", "", head, flags=re.M | re.I).rstrip("\n")
    else:
        head = set_field(set_field(head, "Files-Excluded", ["*"]), "Files-Included", patterns)
    files = [l.strip() for l in open(kept) if l.strip()] + [l.strip() for l in open(debian) if l.strip()]
    hit = last_matches(rest, files)
    used = {}
    for name, (i, p) in hit.items():
        used.setdefault(i, set()).add(p)
    missing = sorted(n for n in files if n not in hit)
    own = set()
    if component:
        for i, par in enumerate(rest):
            f = field(par, "Files")
            if f is not None and all(p.startswith(OWN) for p in f.split()):
                own.add(i)
    if missing:
        die("no Files paragraph covers: " + " ".join(missing[:20]))
    newpars, names = [], set()
    for i, par in enumerate(rest):
        f = field(par, "Files")
        if f is None:
            continue
        if i in own:
            newpars.append(par)
            names.update(licence_names(field(par, "License") or ""))
            continue
        if i not in used:
            continue
        keep = [p for p in f.split() if p in used[i]]
        par = set_field(par, "Files", keep)
        par = prune_holders(par, [n for n, (j, p) in hit.items() if j == i], tree)
        par = re.sub(r"^Files:\n (\S+)$", r"Files: \1", par, count=1, flags=re.M)
        par = re.sub(r"^Files:\n ", "Files: ", par, count=1, flags=re.M)
        newpars.append(par)
        names.update(licence_names(field(par, "License") or ""))
    for par in rest:
        if field(par, "Files") is None and field(par, "License") is not None:
            name = re.sub(r"\s+", " ", (field(par, "License") or "").split("\n", 1)[0]).strip()
            if name in names:
                newpars.append(par)
    open(out or copyright, "w").write("\n\n".join([head] + newpars) + "\n")


def tree_files(tree):
    files = []
    for d, dirs, fs in os.walk(tree):
        dirs[:] = [x for x in dirs if not (d == tree and x in (".pc", ".git"))]
        for f in fs:
            if not (d == tree and f == ".git"):
                files.append(os.path.relpath(os.path.join(d, f), tree))
    generated = os.path.join(tree, "debian", "pie-generated")
    if os.path.exists(generated):
        files += [l.split("#", 1)[0].strip() for l in open(generated) if l.split("#", 1)[0].strip()]
    return files


def merge(copyright, tree, out):
    pars = paragraphs(open(copyright).read())
    files = tree_files(tree)
    total = 0
    while True:
        pars, n = join_once(pars, files)
        total += n
        if n == 0:
            break
    open(out or copyright, "w").write("\n\n".join(pars) + "\n")
    print("joined %d paragraphs; %d Files paragraphs left" % (total, sum(1 for p in pars[1:] if field(p, "Files") is not None)))


def check(copyright, tree):
    pars = paragraphs(open(copyright).read())
    files = tree_files(tree)
    hit = last_matches(pars, files)
    errors = ["no Files paragraph covers " + f for f in sorted(files) if f not in hit]
    effective = {(i, p) for i, p in hit.values()}
    names, texts = set(), set()
    for i, par in enumerate(pars):
        f = field(par, "Files")
        if f is not None:
            for p in f.split():
                if bad_escape(p):
                    errors.append("pattern %r (paragraph %d) has a bad escape" % (p, i))
                if (i, p) not in effective:
                    errors.append("pattern %r (paragraph %d) applies to no file" % (p, i))
            lic = field(par, "License") or ""
            names.update(licence_names(lic))
            if "\n" in lic.strip("\n"):
                texts.update(licence_names(lic))
        elif field(par, "License") is not None and i > 0:
            texts.add(re.sub(r"\s+", " ", field(par, "License").split("\n", 1)[0]).strip())
    for n in sorted(names - texts):
        if n.lower() not in ("public-domain", "none"):
            errors.append("no licence paragraph for " + n)
    for n in sorted(texts - names):
        errors.append("licence paragraph %s is unused" % n)
    for k, place in sorted(joins(pars, files).items()):
        if k != place:
            errors.append("paragraphs %d and %d have the same copyright and licence: join them" % (min(k, place), max(k, place)))
    if errors:
        print("\n".join(errors))
        sys.exit(1)
    print("copyright covers all %d files; every pattern and licence paragraph is used" % len(files))


def main():
    if len(sys.argv) >= 6 and sys.argv[1] == "prune":
        out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else None
        tree = sys.argv[sys.argv.index("--tree") + 1] if "--tree" in sys.argv else None
        prune(*sys.argv[2:6], out, tree, "--component" in sys.argv)
    elif len(sys.argv) in (4, 6) and sys.argv[1] == "merge" and (len(sys.argv) == 4 or sys.argv[4] == "-o"):
        merge(sys.argv[2], sys.argv[3], sys.argv[5] if len(sys.argv) == 6 else None)
    elif len(sys.argv) == 4 and sys.argv[1] == "check":
        check(sys.argv[2], sys.argv[3])
    else:
        die(__doc__)
