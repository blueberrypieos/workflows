# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import re


def bad_escape(pattern):
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            if i + 1 == len(pattern) or pattern[i + 1] not in "*?\\":
                return True
            i += 2
        else:
            i += 1
    return False


def regex(pattern):
    out, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern) and pattern[i + 1] in "*?\\":
            out += re.escape(pattern[i + 1])
            i += 2
            continue
        out += ".*" if ch == "*" else "." if ch == "?" else re.escape(ch)
        i += 1
    return re.compile(out + r"\Z", re.S)


def dep5_matcher(patterns):
    exact, prefixes, rx = set(), [], []
    for p in patterns:
        p = p.rstrip("/")
        if "*" not in p and "?" not in p and "\\" not in p:
            exact.add(p)
            prefixes.append(p + "/")
        elif "\\" not in p and p.endswith("/*") and "*" not in p[:-2] and "?" not in p[:-2]:
            prefixes.append(p[:-1])
        else:
            rx.append(re.compile(regex(p).pattern[:-2] + r"(?:/.*)?\Z", re.S))
    def match(f):
        if f in exact or any(f.startswith(x) for x in prefixes):
            return True
        return any(r.match(f) for r in rx)
    return match


def paragraphs(text):
    return [p for p in re.split(r"\n[ \t]*\n", text.strip("\n")) if p.strip()]


def field(par, name):
    m = re.search(r"^%s:[ \t]*(.*(?:\n[ \t]+.*)*)" % re.escape(name), par, re.M | re.I)
    return m.group(1) if m else None


def licence_names(expr):
    first = re.sub(r"\s+", " ", expr.split("\n", 1)[0]).strip()
    names = []
    for n in re.split(r"\s+(?:and|or|AND|OR)\s+|[(),]", first):
        n = n.strip()
        if n:
            names.append(n)
    return names


def last_matches(pars, files):
    compiled = []
    for i, par in enumerate(pars):
        f = field(par, "Files")
        if f is not None:
            compiled.append((i, [(p, regex(p)) for p in f.split()]))
    hit = {}
    for name in files:
        for i, pats in compiled:
            for p, rx in pats:
                if rx.match(name):
                    hit[name] = (i, p)
    return hit


def set_field(par, name, values):
    if len(values) == 1:
        body = name + ": " + values[0]
    else:
        body = name + ":" + "".join("\n " + v for v in values)
    if field(par, name) is not None:
        return re.sub(r"^%s:[ \t]*.*(?:\n[ \t]+.*)*" % re.escape(name), lambda m: body, par, count=1, flags=re.M | re.I)
    return par + "\n" + body
