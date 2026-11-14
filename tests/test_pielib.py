# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os

import pytest

from pielib.deb822 import control_paragraphs, dependency_names, dsc_fields
from pielib.dep5 import bad_escape, dep5_matcher, field, paragraphs, regex
from pielib.environment import fixed_environment
from pielib.paths import fresh, traversed, within


def test_environment(monkeypatch):
    monkeypatch.setattr(os, 'environ', {'PATH': '/usr/bin', 'TMPDIR': '/tmp', 'PIE_TEST': 'yes',
                                      'HOME': '/caller', 'TAR_OPTIONS': 'bad'})
    fixed_environment(('PIE_TEST',))
    assert os.environ == {'PATH': '/usr/bin', 'TMPDIR': '/tmp', 'PIE_TEST': 'yes',
                          'HOME': '/nonexistent', 'LC_ALL': 'C.UTF-8'}


def test_traversal(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'alias').symlink_to('real')
    (tmp_path / 'chain').symlink_to('alias')
    path = str(tmp_path / 'chain/../real/file')
    entries = traversed(path)
    assert str(tmp_path / 'chain') in entries
    assert str(tmp_path / 'alias') in entries
    assert entries[-1] == str(tmp_path / 'real/file')
    (tmp_path / 'loop').symlink_to('loop')
    with pytest.raises(SystemExit, match='pie-test: .*too many links'):
        traversed(str(tmp_path / 'loop'), 'pie-test')


def test_fresh_and_within(tmp_path):
    target = tmp_path / 'target'
    target.write_text('sentinel')
    link = tmp_path / 'link'
    link.symlink_to(target)
    with fresh(str(link)) as stream:
        stream.write('new')
    assert target.read_text() == 'sentinel'
    assert link.read_text() == 'new'
    assert not link.is_symlink()
    assert within('/a/file', '/a')
    assert within('/a', '/a')
    assert within('/a', '/')
    assert not within('/ab', '/a')


@pytest.mark.parametrize('pattern,yes,no', [('src', 'src/a.c', 'src2/a.c'),
                                            ('src/*', 'src/sub/a.c', 'src'),
                                            ('*.c', 'src/a.c/nested', 'src/a.h'),
                                            (r'src\?', 'src?/a.c', 'srcX/a.c')])
def test_source_directory_rules(pattern, yes, no):
    match = dep5_matcher([pattern])
    assert match(yes)
    assert not match(no)
    assert not dep5_matcher([])(yes)


def test_raw_fields():
    text = 'Files:\n a\n b\nCopyright: 2026 A\n\nLicense: MIT\n .\n text\n'
    pars = paragraphs(text)
    assert field(pars[0], 'files') == '\n a\n b'
    assert field(pars[1], 'License') == 'MIT\n .\n text'
    assert field(pars[0], 'Missing') is None


@pytest.mark.parametrize('pattern', ['x\\', r'x\z'])
def test_bad_escapes_keep_check_diagnostics(pattern):
    assert bad_escape(pattern)
    assert regex(pattern).match(pattern)


def test_deb822(tmp_path):
    dsc = tmp_path / 'test.dsc'
    dsc.write_text('Format: 3.0 (quilt)\nSource: test\nVersion: 1:2-3\nFiles:\n abc 5 test.tar.xz\n def 6 test.debian.tar.xz\n')
    assert dsc_fields(dsc) == ('test', '1:2-3', ['test.tar.xz', 'test.debian.tar.xz'])
    control = 'Source: test\nBuild-Depends: one:any (>= 1),\n two [amd64] | three <!nocheck>\n\nPackage: test-bin\nArchitecture: any\n'
    pars = control_paragraphs(control)
    assert pars[1]['Package'] == 'test-bin'
    assert dependency_names(pars[0]['Build-Depends']) == {'one', 'two', 'three'}


def test_merging_preserves_overrides():
    from pielib.copyright import join_all, rest_of
    from pielib.dep5 import last_matches
    pars = ['Format: test', 'Files: *\nCopyright: A\nLicense: MIT',
            'Files: special/*\nCopyright: B\nLicense: BSD-2-Clause',
            'Files: special/override\nCopyright: A\nLicense: MIT']
    files = ['ordinary', 'special/ordinary', 'special/override']
    merged = join_all(pars, files)
    before = {f: rest_of(pars[i]) for f, (i, p) in last_matches(pars, files).items()}
    after = {f: rest_of(merged[i]) for f, (i, p) in last_matches(merged, files).items()}
    assert before == after
    assert merged == pars
    mergeable = [pars[0], pars[1], 'Files: second\nCopyright: A\nLicense: MIT']
    assert len(join_all(mergeable, ['ordinary', 'second'])) == 2
