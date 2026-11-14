# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import random
import runpy
from pathlib import Path

import pytest

from pielib.copyright import join_all, joins, rest_of
from pielib.dep5 import last_matches, paragraphs, regex


def test_sdk_merge_parity(request):
    checkout = request.config.getoption('--pietoolchain')
    if not checkout:
        pytest.skip('pass --pietoolchain PATH to compare the SDK copy')
    sdk = runpy.run_path(str(Path(checkout) / 'sdk/mksrccopyright.py'))
    patterns = ['*', 'src/*', '*.c', 'src/a.c', r'src/file\?.c', r'src/file\*.c',
                r'src/file\\.c', 'absent', 'src/*/*', '?.c']
    files = ['src/a.c', 'src/b.c', 'src/sub/a.c', 'src/file?.c', 'src/file*.c',
             'src/file\\.c', 'a.c', 'README', 'line\nbreak']
    for pattern in patterns:
        assert [bool(regex(pattern).match(f)) for f in files] == [
            bool(sdk['dep5_regex'](pattern).match(f)) for f in files]
    rng = random.Random(20261113)
    for _ in range(300):
        pars = ['Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/']
        for _ in range(rng.randint(2, 9)):
            pats = rng.sample(patterns, rng.randint(1, 4))
            pars.append('Files: ' + '\n '.join(pats) + '\nCopyright: ' + rng.choice(['2026 A', '2025 B']) +
                        '\nLicense: ' + rng.choice(['BSD-2-Clause', 'MIT']) + '\n body\n .\n text')
        assert joins(pars, files) == sdk['joins'](pars, files)
        merged = join_all(pars, files)
        assert merged == sdk['join_all'](pars, files)
        assert join_all(merged, files) == merged
        before = {f: rest_of(pars[i]) for f, (i, p) in last_matches(pars, files).items()}
        after = {f: rest_of(merged[i]) for f, (i, p) in last_matches(merged, files).items()}
        assert before == after
