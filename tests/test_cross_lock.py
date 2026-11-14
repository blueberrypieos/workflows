# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import hashlib
import runpy
import subprocess

import pytest

from conftest import TOOLS
from pielib.cross_lock import check_lock, lock_text, read_lock
from pielib.cross_lock import retained_lock_required
from test_cross_record import archive


def test_sorted_tab_separated_records(tmp_path):
    records = [('host', 'z-dev', 'darwin-arm64', '2', 'b' * 64),
               ('build', 'make', 'amd64', '4.4', 'a' * 64),
               ('host', 'a-dev', 'darwin-arm64', '1', 'c' * 64)]
    path = tmp_path / 'cross-deps.lock'
    path.write_text(lock_text(records))
    assert read_lock(path) == sorted(records)
    assert path.read_text().startswith('build\tmake\tamd64\t4.4\t' + 'a' * 64 + '\n')
    path.write_text(''.join('\t'.join(row) + '\n' for row in records))
    with pytest.raises(ValueError, match='sorted'):
        read_lock(path)


@pytest.mark.parametrize('change', ['equal', 'version', 'hash', 'added', 'removed'])
def test_compare_names_changed_or_missing_package(tmp_path, change):
    before = [('host', 'example-dev', 'darwin-arm64', '1', 'a' * 64)]
    after = before.copy()
    if change == 'version':
        after[0] = (*before[0][:3], '2', before[0][4])
    elif change == 'hash':
        after[0] = (*before[0][:4], 'b' * 64)
    elif change == 'added':
        after.append(('build', 'example-build', 'all', '1', 'c' * 64))
    elif change == 'removed':
        after = []
    full, reduced = tmp_path / 'full.lock', tmp_path / 'reduced.lock'
    full.write_text(lock_text(before))
    reduced.write_text(lock_text(after))
    if change == 'equal':
        check_lock(reduced, full)
    else:
        with pytest.raises(ValueError, match='example') as error:
            check_lock(reduced, full)
        assert '--- ' + str(full) in str(error.value)
        assert '+++ ' + str(reduced) in str(error.value)


def test_base_is_separate_and_actual_native_archives_are_required(archive, tmp_path):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'status').write_text('')
    evidence = tmp_path / 'evidence'
    namespace['capture'](state, evidence)
    cache = tmp_path / 'cache'
    cache.mkdir()
    tree = tmp_path / 'package'
    control = tree / 'DEBIAN/control'
    control.write_text(control.read_text().replace('darwin-arm64', 'amd64'))
    native = cache / 'native.deb'
    subprocess.run(['dpkg-deb', '--build', str(tree), str(native)], check=True, capture_output=True)
    before = {('already-installed', 'amd64'): '3', ('example', 'amd64'): '1.0'}
    after = before.copy()
    after[('example', 'amd64')] = '1.2'
    namespace['write_locks'](evidence, before, after, cache, [])
    assert (evidence / 'base.lock').read_text() == 'build\talready-installed\tamd64\t3\nbuild\texample\tamd64\t1.0\n'
    sha = hashlib.sha256(native.read_bytes()).hexdigest()
    assert read_lock(evidence / 'cross-deps.lock') == [('build', 'example', 'amd64', '1.2', sha)]
    native.unlink()
    with pytest.raises(ValueError, match='retained archive: example'):
        namespace['write_locks'](evidence, before, after, cache, [])


def test_unchanged_base_needs_no_dependency_archive(tmp_path):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    (evidence / 'receipt.json').write_text('{"packages": []}')
    cache = tmp_path / 'empty-cache'
    cache.mkdir()
    before = {('base-tool', 'amd64'): '1'}
    namespace['write_locks'](evidence, before, before.copy(), cache, [])
    assert (evidence / 'cross-deps.lock').read_text() == ''
    assert (evidence / 'base.lock').read_text() == 'build\tbase-tool\tamd64\t1\n'


def test_native_apt_keeps_exact_downloads_without_new_resolution(tmp_path, monkeypatch):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: calls.append((argv, kwargs)))
    observer = namespace['ObservedProcesses'](tmp_path)
    observer.run(['apt-get', 'satisfy', '-y', 'example'], check=True)
    observer.run(['apt-get', '-o', 'APT::Architecture=darwin-arm64', 'download', 'target=1'])
    assert len(calls) == 2
    assert calls[0][0][-3:] == ['satisfy', '-y', 'example']
    assert 'Dir::Cache::archives=' + str(tmp_path) in calls[0][0]
    assert 'APT::Keep-Downloaded-Packages=true' in calls[0][0]
    assert calls[1][0] == ['apt-get', '-o', 'APT::Architecture=darwin-arm64', 'download', 'target=1']


@pytest.mark.parametrize('side', ['F', 'R'])
@pytest.mark.parametrize('evidence', ['lock', 'base', 'metadata', 'log', 'directory', 'broken-link', 'none'])
def test_retained_evidence_determines_lock_requirement(tmp_path, side, evidence):
    import json
    traces = [tmp_path / name for name in ('F', 'R')]
    for trace in traces:
        trace.mkdir()
        (trace / 'meta.json').write_text(json.dumps({'cross_deps': False}))
        (trace / 'cross-deps.log').write_text('')
    trace = tmp_path / side
    if evidence == 'lock':
        (trace / 'cross-deps.lock').write_text('')
    elif evidence == 'base':
        (trace / 'base.lock').write_text('')
    elif evidence == 'metadata':
        (trace / 'meta.json').write_text(json.dumps({'cross_deps': True}))
    elif evidence == 'log':
        (trace / 'cross-deps.log').write_text('dependency preparation ran\n')
    elif evidence == 'directory':
        (trace / 'cross-deps').mkdir()
    elif evidence == 'broken-link':
        (trace / 'cross-deps.lock').symlink_to('missing')
    assert retained_lock_required(traces) == (evidence != 'none')
