# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: read_lock(FILE), lock_text(RECORDS), check_lock(ACTUAL, EXPECTED)."""
import difflib
import json
from pathlib import Path
import re


def lock_text(records):
    rows = sorted(tuple(record) for record in records)
    seen = set()
    for row in rows:
        if (len(row) != 5 or row[0] not in ('build', 'host')
                or any(not isinstance(value, str) or not value or any(c.isspace() for c in value)
                       for value in row)
                or not re.fullmatch('[0-9a-f]{64}', row[4])):
            raise ValueError('invalid cross-deps.lock record: ' + repr(row))
        key = row[:3]
        if key in seen:
            raise ValueError('duplicate cross-deps.lock package: ' + row[1])
        seen.add(key)
    return ''.join('\t'.join(row) + '\n' for row in rows)


def read_lock(path):
    data = Path(path).read_text()
    records = [tuple(line.split('\t')) for line in data.splitlines()]
    if lock_text(records) != data:
        raise ValueError('cross-deps.lock must be sorted and tab-separated: ' + str(path))
    return records


def check_lock(actual, expected):
    actual_text = lock_text(read_lock(actual))
    expected_text = lock_text(read_lock(expected))
    if actual_text != expected_text:
        diff = ''.join(difflib.unified_diff(expected_text.splitlines(True), actual_text.splitlines(True),
                                            fromfile=str(expected), tofile=str(actual)))
        raise ValueError('cross dependency lock differs:\n' + diff)


def retained_lock_required(traces):
    for trace in map(Path, traces):
        if any(path.exists() or path.is_symlink() for path in
               (trace / 'cross-deps.lock', trace / 'base.lock', trace / 'cross-deps')):
            return True
        log = trace / 'cross-deps.log'
        if log.exists() and log.stat().st_size:
            return True
        metadata = trace / 'meta.json'
        if metadata.exists():
            data = json.loads(metadata.read_text())
            if not isinstance(data, dict):
                raise ValueError('invalid trace metadata: ' + str(metadata))
            if data.get('cross_deps'):
                return True
    return False
