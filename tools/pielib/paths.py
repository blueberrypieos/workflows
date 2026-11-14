# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os
import sys


def traversed(path, tool="pie"):
    out, cur, hops = [], "/", 0
    todo = [c for c in os.path.join(os.getcwd(), path).split("/") if c]
    while todo:
        c = todo.pop(0)
        if c == ".":
            continue
        if c == "..":
            cur = os.path.dirname(cur)
            continue
        e = os.path.join(cur, c)
        out.append(e)
        if os.path.islink(e):
            hops += 1
            if hops > 40:
                sys.exit("%s: %s: too many links" % (tool, path))
            t = os.readlink(e)
            todo = [x for x in t.split("/") if x] + todo
            if t.startswith("/"):
                cur = "/"
        else:
            cur = e
    return out


def within(path, top):
    return path == top or top == "/" or path.startswith(top.rstrip("/") + "/")


def fresh(path):
    if os.path.lexists(path):
        os.unlink(path)
    return open(path, "w")
