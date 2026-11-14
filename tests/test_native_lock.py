# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import hashlib
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

from conftest import TOOLS
from pielib.cross_lock import check_lock, read_lock
from test_cross_record import archive


def status_record(name, version='1', architecture='amd64', fields=''):
    return ('Package: ' + name + '\nVersion: ' + version + '\nArchitecture: ' + architecture
            + '\nStatus: install ok installed\n' + fields + '\n\n')


@pytest.fixture
def native_archive(archive, tmp_path):
    control = tmp_path / 'package/DEBIAN/control'
    control.write_text(control.read_text().replace('darwin-arm64', 'amd64'))
    native = tmp_path / 'native.deb'
    subprocess.run(['dpkg-deb', '--build', str(control.parent.parent), str(native)], check=True, capture_output=True)
    return native


@pytest.mark.parametrize('backend', ['build-dep', 'helper'])
@pytest.mark.parametrize('retained_archive', [True, False])
def test_shared_root_records_needed_native_packages_on_both_sides(native_archive, tmp_path, monkeypatch, backend, retained_archive):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))['main'].__globals__
    native = native_archive
    source = tmp_path / 'source'
    (source / 'debian').mkdir(parents=True)
    (source / 'debian/control').write_text('# source metadata\nSource: test\nBuild-Depends: example (>= 1)\n\nPackage: test\n')
    helper = tmp_path / 'resolver.py'
    helper.write_text('''import os, pathlib, shutil, subprocess, tempfile
def main():
    temporary = tempfile.mkdtemp()
    try:
        pathlib.Path(temporary, 'status').write_text('')
        subprocess.run(['apt-get', 'satisfy', '-y', '--no-install-recommends', 'example (>= 1)'],
                       check=True, env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))
    finally:
        shutil.rmtree(temporary)
''')
    status = tmp_path / 'status'
    initial = status_record('unrelated-base')
    installed = initial + status_record('example', '1.2')
    status.write_text(initial)
    available = tmp_path / 'global-cache'
    available.mkdir()
    monkeypatch.setitem(namespace, 'DPKG_STATUS', status)
    monkeypatch.setitem(namespace, 'APT_ARCHIVES', available)
    original = subprocess.run
    calls = []
    def run(argv, **kwargs):
        if argv[0] != 'apt-get':
            return original(argv, **kwargs)
        calls.append(argv)
        if 'satisfy' in argv or 'build-dep' in argv:
            assert kwargs['env']['DEBIAN_FRONTEND'] == 'noninteractive'
            cache = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('Dir::Cache::archives=')))
            if status.read_text() == initial:
                shutil.copyfile(native, cache / 'installed.deb')
            status.write_text(installed)
        else:
            assert argv == ['apt-get', 'download', '-q', 'example:amd64=1.2']
            shutil.copyfile(native, Path(kwargs['cwd']) / 'installed.deb')
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', run)
    for side in ('F', 'R'):
        reference = ['--cross-deps-lock', str(tmp_path / 'F/cross-deps.lock')] if side == 'R' else []
        if side == 'R' and not retained_archive:
            for path in (tmp_path / 'F/archives').glob('*.deb'):
                path.unlink()
        monkeypatch.setattr(sys, 'argv', [str(TOOLS / 'pie-cross-record'), *reference, '--build-dep' if backend == 'build-dep' else str(helper),
                                        str(source), str(tmp_path / side)])
        namespace['main']()
    check_lock(tmp_path / 'R/cross-deps.lock', tmp_path / 'F/cross-deps.lock')
    digest = hashlib.sha256(native.read_bytes()).hexdigest()
    assert read_lock(tmp_path / 'F/cross-deps.lock') == [('build', 'example', 'amd64', '1.2', digest)]
    for side in (('F', 'R') if retained_archive else ('R',)):
        assert (tmp_path / side / 'archives' / (digest + '.deb')).read_bytes() == native.read_bytes()
    assert 'example' not in (tmp_path / 'F/base.lock').read_text()
    assert 'example' in (tmp_path / 'R/base.lock').read_text()
    assert sum(argv[:3] == ['apt-get', 'download', '-q'] for argv in calls) == (0 if retained_archive else 1)


def test_reference_archive_must_match_identity_and_hash(archive, tmp_path):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    lock = tmp_path / 'reference.lock'
    cache = tmp_path / 'native'
    cache.mkdir()
    path = tmp_path / 'archives' / (digest + '.deb')
    path.parent.mkdir()
    shutil.copyfile(archive, path)
    expected = [('build', 'example', 'amd64', '1.2', digest)]
    with pytest.raises(ValueError, match='reference build dependency archive differs: example'):
        namespace['copy_reference'](lock, expected, cache)
    expected = [('build', 'example', 'darwin-arm64', '1.2', digest)]
    path.write_bytes(b'corrupt archive')
    with pytest.raises(ValueError, match='reference build dependency archive differs: example'):
        namespace['copy_reference'](lock, expected, cache)
    assert not list(cache.iterdir())


def test_reference_native_version_is_read_from_installed_state(tmp_path):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))
    expected = [('build', 'example', 'amd64', '1', 'a' * 64)]
    after = {('example', 'amd64'): '2', ('unrelated-base', 'amd64'): '7'}
    assert namespace['native_selection'](after, after, expected) == {('example', 'amd64')}
    assert namespace['native_selection']({}, {}, expected) == set()


@pytest.mark.parametrize('reference_version', ['1.0', '1.2'])
def test_reference_does_not_replace_actual_native_version_or_hash(native_archive, tmp_path, reference_version):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'status').write_text('')
    evidence = tmp_path / 'evidence'
    namespace['capture'](state, evidence)
    cache = tmp_path / 'cache'
    cache.mkdir()
    shutil.copyfile(native_archive, cache / 'actual.deb')
    expected = [('build', 'example', 'amd64', reference_version, 'a' * 64)]
    installed = {('example', 'amd64'): '1.2'}
    namespace['write_locks'](evidence, installed, installed, cache, expected)
    actual = read_lock(evidence / 'cross-deps.lock')
    assert actual == [('build', 'example', 'amd64', '1.2', hashlib.sha256(native_archive.read_bytes()).hexdigest())]


def test_reference_archive_input_cannot_be_removed_with_output(tool, tmp_path, monkeypatch):
    trace = tool('trace')
    namespace = trace['run_trace'].__globals__
    root = tmp_path / 'root'
    root.mkdir()
    out = tmp_path / 'output'
    out.mkdir()
    payload = out / 'native.deb'
    payload.write_bytes(b'native archive')
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    reference = tmp_path / 'reference'
    (reference / 'archives').mkdir(parents=True)
    (reference / 'archives' / (digest + '.deb')).symlink_to(payload)
    lock = reference / 'cross-deps.lock'
    lock.write_text('build\texample\tamd64\t1\t' + digest + '\n')
    source = tmp_path / 'test.dsc'
    source.write_text('source package')
    monkeypatch.setitem(namespace, 'dsc_fields', lambda path: ('test', '1', []))
    monkeypatch.setitem(namespace, 'sudo', lambda *a, **kw: pytest.fail('input checks must precede root mutations'))
    with pytest.raises(SystemExit, match='on the way'):
        trace['run_trace'](str(root), str(source), str(out), 1, True, 'test', cross_deps_lock=str(lock))
    assert payload.read_bytes() == b'native archive'
