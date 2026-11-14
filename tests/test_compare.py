# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import TOOLS
from pielib.commands import compare


@pytest.fixture
def deb(tmp_path):
    def build(directory, name='sample', version='1', architecture='all', payload=b'first\n',
              fields='', mode=0o644, mtime=1700000000, script=b'#!/bin/sh\nexit 0\n',
              files=None, link='file'):
        directory.mkdir(parents=True, exist_ok=True)
        root = tmp_path / ('root-%d' % len(list(tmp_path.glob('root-*'))))
        (root / 'DEBIAN').mkdir(parents=True)
        (root / 'usr/share/sample').mkdir(parents=True)
        (root / 'DEBIAN/control').write_text(
            'Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: Example <example@example.org>\n'
            'Description: sample\n sample details\n%s' % (name, version, architecture, fields))
        (root / 'DEBIAN/postinst').write_bytes(script)
        (root / 'DEBIAN/postinst').chmod(0o755)
        (root / 'usr/share/sample/file').write_bytes(payload)
        (root / 'usr/share/sample/file').chmod(mode)
        os.link(root / 'usr/share/sample/file', root / 'usr/share/sample/hardlink')
        (root / 'usr/share/sample/link').symlink_to(link)
        for filename, body in (files or {}).items():
            (root / 'usr/share/sample' / filename).write_bytes(body)
        for path in [*root.rglob('*'), root]:
            os.utime(path, (mtime, mtime), follow_symlinks=False)
        path = directory / ('%s_%s_%s.deb' % (name, version, architecture))
        subprocess.run(['dpkg-deb', '--root-owner-group', '-b', str(root), str(path)], check=True,
                       capture_output=True, env=dict(os.environ, SOURCE_DATE_EPOCH='1800000000'))
        return path
    return build


@pytest.fixture
def diagnostics(monkeypatch):
    seen = []
    def run(command, optional=False):
        seen.append(command)
        return dict(command=command, status='completed', returncode=1,
                    stdout='full diagnostic\n', stderr='', error=False)
    monkeypatch.setattr(compare, 'diagnostic', run)
    return seen


def cli(*args, cwd=None):
    return subprocess.run([str(TOOLS / 'pie'), 'compare', *map(str, args)],
                          cwd=cwd, capture_output=True, text=True)


def test_bytes_identical_and_hashes(deb, tmp_path):
    a = deb(tmp_path / 'old')
    b = deb(tmp_path / 'new')
    report, lines, status = compare.compare_bytes(a.parent, b.parent)
    assert status == 0
    assert report['identical'] == [a.name]
    assert report['old'][a.name] == hashlib.sha256(a.read_bytes()).hexdigest()
    assert '1 identical' in lines[0]


def test_bytes_missing_extra_and_different(deb, tmp_path):
    a = deb(tmp_path / 'old')
    b = deb(tmp_path / 'new', payload=b'changed\n')
    missing = deb(a.parent, name='missing')
    extra = deb(b.parent, name='extra')
    (a.parent / 'build.log').write_text('unrelated')
    report, _, status = compare.compare_bytes(a.parent, b.parent)
    assert status == 1
    assert report['missing'] == [missing.name]
    assert report['extra'] == [extra.name]
    assert report['different'] == [a.name]


def test_bytes_never_ignores_control_fields_or_names(deb, tmp_path):
    a = deb(tmp_path / 'old', fields='Priority: optional\n')
    b = deb(tmp_path / 'new', fields='Priority: required\n')
    assert compare.compare_bytes(a, b)[2] == 1
    shutil.copyfile(a, b)
    renamed = b.with_name('renamed.deb')
    b.rename(renamed)
    report, _, status = compare.compare_bytes(a, renamed)
    assert status == 1
    assert report['missing'] == [a.name]
    assert report['extra'] == [renamed.name]


@pytest.mark.parametrize('kind', ['empty', 'absent', 'bad-entry'])
def test_bytes_invalid_inputs(tmp_path, kind):
    directory = tmp_path / 'debs'
    if kind != 'absent':
        directory.mkdir()
    if kind == 'bad-entry':
        (directory / 'not-a-file.deb').mkdir()
    result = cli(directory, directory, '--mode', 'bytes')
    assert result.returncode == 2
    assert 'pie compare:' in result.stderr
    assert 'Traceback' not in result.stderr


def test_package_complete_diagnostics(deb, diagnostics, tmp_path):
    a = deb(tmp_path / 'old', fields='Priority: optional\nInstalled-Size: 1\nX-Removed: old\n')
    b = deb(tmp_path / 'new', version='2', payload=b'second\n', mode=0o755,
            fields='Priority: required\nInstalled-Size: 2\nX-Added: new\n', script=b'#!/bin/sh\nexit 1\n')
    report, lines, status = compare.compare_packages(a.parent, b.parent)
    assert status == 1
    pair, = report['pairs']
    assert pair['fields'] == {
        'Installed-Size': {'old': '1', 'new': '2'},
        'Priority': {'old': 'optional', 'new': 'required'},
        'Version': {'old': '1', 'new': '2'},
        'X-Added': {'old': None, 'new': 'new'},
        'X-Removed': {'old': 'old', 'new': None}}
    old = pair['old']['data']['usr/share/sample/file']
    new = pair['new']['data']['usr/share/sample/file']
    assert old['mode'] == '0644' and new['mode'] == '0755'
    assert old['uid'] == old['gid'] == 0
    assert old['sha256'] == hashlib.sha256(b'first\n').hexdigest()
    assert new['sha256'] == hashlib.sha256(b'second\n').hexdigest()
    assert pair['new']['data']['usr/share/sample/hardlink']['sha256'] == new['sha256']
    assert pair['new']['data']['usr/share/sample/link']['link'] == 'file'
    assert 'postinst' in pair['control']
    assert 'usr/share/sample/file' in pair['data']
    assert len(diagnostics) == 2
    assert diagnostics[0][0] == 'debdiff'
    assert diagnostics[1][:3] == ['diffoscope', '--text', '-']
    assert pair['diagnostics']['diffoscope']['stdout'] == 'full diagnostic\n'
    assert 'Priority' in '\n'.join(lines)


def test_package_identical_needs_no_external_diagnostics(deb, diagnostics, tmp_path):
    a = deb(tmp_path / 'old')
    b = deb(tmp_path / 'new')
    report, _, status = compare.compare_packages(a, b)
    assert status == 0
    assert not report['pairs'][0]['fields']
    assert not report['pairs'][0]['data']
    assert diagnostics == []


def test_package_reports_mtime_only_changes(deb, diagnostics, tmp_path):
    a = deb(tmp_path / 'old')
    b = deb(tmp_path / 'new', mtime=1700000001)
    report, _, status = compare.compare_packages(a, b)
    assert status == 1
    pair, = report['pairs']
    assert pair['fields'] == {}
    changed = pair['data']['usr/share/sample/file']
    assert changed['old']['sha256'] == changed['new']['sha256']
    assert changed['old']['mtime'] != changed['new']['mtime']


def test_package_reports_added_removed_files_and_symlinks(deb, diagnostics, tmp_path):
    a = deb(tmp_path / 'old', files={'removed': b'gone'})
    b = deb(tmp_path / 'new', files={'added': b'new'}, link='hardlink')
    report, _, status = compare.compare_packages(a, b)
    assert status == 1
    pair, = report['pairs']
    assert pair['data']['usr/share/sample/removed']['new'] is None
    assert pair['data']['usr/share/sample/added']['old'] is None
    assert pair['data']['usr/share/sample/link']['new']['link'] == 'hardlink'
    assert pair['data']['usr/share/sample/link']['new']['sha256'] is None


def test_explicit_debs_compare_package_identity(deb, diagnostics, tmp_path):
    a = deb(tmp_path / 'old', name='one')
    b = deb(tmp_path / 'new', name='two', architecture='amd64')
    report, _, status = compare.compare_packages(a, b)
    assert status == 1
    assert set(report['pairs'][0]['fields']) == {'Package', 'Architecture'}
    report, _, status = compare.compare_packages(a.parent, b.parent)
    assert status == 1 and report['pairs'] == []
    assert report['missing'][0]['filename'] == a.name
    assert report['extra'][0]['filename'] == b.name


def test_ambiguous_packages_fail(deb, tmp_path):
    a = deb(tmp_path / 'old')
    deb(a.parent, version='2')
    b = deb(tmp_path / 'new')
    with pytest.raises(ValueError, match='ambiguous package'):
        compare.compare_packages(a.parent, b.parent)


def test_corrupt_package_fails_without_traceback(tmp_path):
    path = tmp_path / 'corrupt.deb'
    path.write_bytes(b'not a deb')
    result = cli(path, path, '--mode', 'package')
    assert result.returncode == 2
    assert 'dpkg-deb:' in result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('optional', [False, True])
def test_diagnostic_tool_unavailable(monkeypatch, optional):
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    result = compare.diagnostic(['tool'], optional=optional)
    assert result['status'] == 'unavailable'
    assert result['error'] is not optional
    assert 'not installed' in result['stderr']


@pytest.mark.parametrize('code', [0, 1, 2])
def test_diagnostic_preserves_output_and_failure(monkeypatch, code):
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/tool')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], code, 'all output', 'all errors'))
    result = compare.diagnostic(['tool'])
    assert result['stdout'] == 'all output' and result['stderr'] == 'all errors'
    assert result['error'] == (code == 2)


def test_failed_diagnostic_is_an_error(deb, monkeypatch, tmp_path):
    a = deb(tmp_path / 'old')
    b = deb(tmp_path / 'new', payload=b'changed')
    monkeypatch.setattr(compare, 'diagnostic', lambda *args, **kwargs:
                        dict(status='failed', stderr='failed to compare', error=True))
    _, lines, status = compare.compare_packages(a, b)
    assert status == 2
    assert 'failed to compare' in '\n'.join(lines)


@pytest.mark.parametrize('existing', [False, True])
def test_output_report_and_existing_directory(deb, tmp_path, existing):
    a = deb(tmp_path / 'old')
    b = deb(tmp_path / 'new')
    output = tmp_path / 'output'
    if existing:
        output.mkdir()
        (output / 'unrelated').write_text('kept')
    result = cli(a, b, '--mode', 'package', '--out', output)
    assert result.returncode == 0, result.stderr
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 0
    assert report['pairs'][0]['old']['data']['usr/share/sample/file']['sha256']
    assert result.stdout == (output / 'summary.txt').read_text()
    second = cli(a, b, '--mode', 'package', '--out', output)
    assert second.returncode == 2
    assert json.loads((output / 'report.json').read_text()) == report
    if existing:
        assert (output / 'unrelated').read_text() == 'kept'


def test_report_without_out_is_complete(deb, tmp_path):
    a = deb(tmp_path / 'old')
    result = cli(a, a, '--mode', 'bytes', '--json')
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report['identical'] == [a.name]


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_tree_refs_and_tree_ids(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git('init', '--quiet', cwd=repo)
    git('config', 'user.name', 'Example', cwd=repo)
    git('config', 'user.email', 'example@example.org', cwd=repo)
    file = repo / 'file'
    file.write_text('old\n')
    git('add', '.', cwd=repo)
    git('commit', '-qm', 'first', cwd=repo)
    old = git('rev-parse', 'HEAD^{tree}', cwd=repo)
    file.write_text('new\n')
    git('commit', '-qam', 'second', cwd=repo)
    new = git('rev-parse', 'HEAD^{tree}', cwd=repo)
    result = cli('HEAD~', 'HEAD', '--mode', 'tree', cwd=repo)
    assert result.returncode == 1
    assert old in result.stdout and new in result.stdout
    assert 'file |' in result.stdout
    assert cli(old, old, '--mode', 'tree', cwd=repo).returncode == 0
    assert cli('unknown-ref', 'HEAD', '--mode', 'tree', cwd=repo).returncode == 2


def test_tree_directory_snapshots_are_git_trees_and_leave_inputs_untouched(tmp_path):
    a, b = tmp_path / 'old', tmp_path / 'new'
    a.mkdir()
    (a / 'file').write_text('content\n')
    (a / 'executable').write_text('run\n')
    (a / 'executable').chmod(0o755)
    (a / 'group-exec').write_text('data\n')
    (a / 'group-exec').chmod(0o654)
    (a / 'link').symlink_to('file')
    (a / 'empty').mkdir()
    git('init', '--quiet', cwd=a)
    git('add', '.', cwd=a)
    expected = git('write-tree', cwd=a)
    before = (a / '.git/index').read_bytes()
    shutil.copytree(a, b, symlinks=True)
    result = cli(a, b, '--mode', 'tree', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert (a / '.git/index').read_bytes() == before
    (b / 'file').chmod(0o755)
    (b / 'ignored').write_text('included\n')
    (b / '.gitignore').write_text('ignored\n')
    result = cli(a, b, '--mode', 'tree', cwd=tmp_path)
    assert result.returncode == 1
    assert re.search(r'ignored\s+\|', result.stdout)
    assert (a / '.git/index').read_bytes() == before


def test_tree_empty_directories_and_mixed_inputs(tmp_path):
    a, b = tmp_path / 'old', tmp_path / 'new'
    a.mkdir()
    b.mkdir()
    assert cli(a, b, '--mode', 'tree').returncode == 0
    result = cli(a, 'HEAD', '--mode', 'tree')
    assert result.returncode == 2
    assert 'two directories or two refs' in result.stderr


def test_tree_temporary_objects_stay_outside_inputs(tmp_path, monkeypatch):
    a, b = tmp_path / 'old', tmp_path / 'new'
    (a / 'scratch').mkdir(parents=True)
    b.mkdir()
    monkeypatch.setenv('TMPDIR', str(a / 'scratch'))
    result = cli(a, b, '--mode', 'tree')
    assert result.returncode == 0, result.stderr
    assert list((a / 'scratch').iterdir()) == []


def test_tree_rejects_special_files(tmp_path):
    a, b = tmp_path / 'old', tmp_path / 'new'
    a.mkdir()
    b.mkdir()
    os.mkfifo(a / 'fifo')
    result = cli(a, b, '--mode', 'tree')
    assert result.returncode == 2
    assert 'not a regular file' in result.stderr
