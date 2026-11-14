# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import re

from .dep5 import field, regex, set_field, last_matches


def rest_of(par):
    body = re.sub(r"^Files:[ \t]*.*(?:\n[ \t]+.*)*\n?", "", par, count=1, flags=re.M | re.I)
    return "\n".join(l.rstrip() for l in body.strip("\n").split("\n"))


def joins(pars, files):
    idx = [i for i, par in enumerate(pars) if i > 0 and field(par, "Files") is not None]
    rx = {i: [regex(p) for p in field(pars[i], "Files").split()] for i in idx}
    key = {i: rest_of(pars[i]) for i in idx}
    matches = {}
    for name in files:
        m = tuple(i for i in idx if any(r.match(name) for r in rx[i]))
        if m:
            matches[m] = True
    by_par = {}
    for m in matches:
        for i in m:
            by_par.setdefault(i, []).append(m)
    at = {i: i for i in idx}

    def keeps(src, dst):
        moving = {k for k in idx if at[k] in (src, dst)}
        for i in moving:
            for m in by_par.get(i, []):
                old = max(m, key=lambda k: at[k])
                new = max(m, key=lambda k: dst if k in moving else at[k])
                if key[old] != key[new]:
                    return False
        return True

    while True:
        places = sorted(set(at.values()))
        pair = next(((src, dst) for x, a in enumerate(places) for b in places[x + 1:] if key[a] == key[b]
                     for src, dst in ((b, a), (a, b)) if keeps(src, dst)), None)
        if pair is None:
            return at
        for k in idx:
            if at[k] == pair[0]:
                at[k] = pair[1]


def covered(names, files):
    keep = list(names)
    for p in sorted(names, key=lambda n: (n.count("*") + n.count("?"), n)):
        own = [f for f in files if regex(p).match(f)]
        others = [regex(q) for q in keep if q != p]
        if own and all(any(r.match(f) for r in others) for f in own):
            keep.remove(p)
    return keep


def join_once(pars, files):
    at = joins(pars, files)
    names = {}
    for k, place in at.items():
        names.setdefault(place, []).extend(field(pars[k], "Files").split())
    newpars, joined = [], []
    for k, par in enumerate(pars):
        if k in at and at[k] != k:
            continue
        if k in at and len(names[k]) > len(field(par, "Files").split()):
            par = set_field(par, "Files", covered(sorted(set(names[k])), files))
            joined.append(len(newpars))
        newpars.append(par)
    effective = {(i, p) for i, p in last_matches(newpars, files).values()}
    for k in joined:
        keep = [p for p in field(newpars[k], "Files").split()
                if (k, p) in effective or not any(regex(p).match(f) for f in files)]
        par = set_field(newpars[k], "Files", keep)
        newpars[k] = re.sub(r"^Files:\n ", "Files: ", par, count=1, flags=re.M)
    return newpars, sum(1 for k in at if at[k] != k)


def join_all(pars, files):
    while True:
        pars, n = join_once(pars, files)
        if n == 0:
            return pars
