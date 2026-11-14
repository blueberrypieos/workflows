# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
from debian.deb822 import Deb822, Dsc, PkgRelation


def control_paragraphs(text):
    return list(Deb822.iter_paragraphs(text.splitlines(True), use_apt_pkg=False))


def dependency_names(value):
    if not value.strip():
        return set()
    return {dep["name"].split(":")[0] for group in PkgRelation.parse_relations(value) for dep in group}


def dsc_fields(path):
    with open(path) as stream:
        fields = Dsc(stream)
    return fields["Source"], fields["Version"], [entry["name"] for entry in fields["Files"]]


def dsc_files(path):
    return dsc_fields(path)[2]
