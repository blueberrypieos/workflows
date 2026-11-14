# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import hashlib
import json
import os
import shutil
import sys
import tarfile

import pytest

from pielib.commands import compare, verify
from pielib.dep5 import regex
from test_compare import cli, deb, diagnostics, git
from test_compare_archives import archive, invoke, probe_deb, regular, trace_output


def corrupt_header(raw, index):
    offset = 0
    for _ in range(index):
        size = int(raw[offset + 124:offset + 136].rstrip(b'\0 ') or b'0', 8)
        offset += 512 + ((size + 511) // 512) * 512
    return raw[:offset + 148] + b'0000000\0' + raw[offset + 156:]


@pytest.mark.parametrize('part', ['data', 'control'])
@pytest.mark.parametrize('damage', ['bad-header', 'after-end', 'short-header'])
def test_corrupt_tar_never_hides_later_members(tmp_path, part, damage):
    entries = [regular('./control', b'Package: sample\nVersion: 1\nArchitecture: all\n')]
    entries += [regular('./one', b'one'), regular('./two', b'two'), regular('./third', b'third'),
                ('./evil', b'', tarfile.SYMTYPE, '/etc'), regular('./usr/c', b'hidden')]
    raw = archive(entries)
    if damage == 'bad-header':
        raw = corrupt_header(raw, 3)
    elif damage == 'after-end':
        raw += archive([regular('hidden-after-end', b'hidden')])
    else:
        raw = raw[:2048] + b'partial header'
    path = probe_deb(tmp_path / 'corrupt.deb', **{part + '_tar': raw})
    for mode in ('bytes', 'package'):
        result = invoke('compare', path, path, '--mode', mode)
        assert result.returncode == 2
        assert str(path) in result.stderr and 'Traceback' not in result.stderr
    full, reduced = [trace_output(tmp_path / label, path) for label in ('full', 'reduced')]
    result = invoke('verify', 'compare', full, reduced)
    assert result.returncode == 1
    assert 'archive' in result.stdout


def test_allow_missing_preserves_member_name_spelling(tmp_path):
    full = probe_deb(tmp_path / 'full.deb', data_tar=archive([
        regular('./usr/bin/x', b'same'), regular('./usr/bin/gone', b'gone')]))
    reduced = probe_deb(tmp_path / 'reduced.deb', data_tar=archive([
        regular('./usr//bin/./x', b'same')]))
    bad, gone = verify.deb_diff(str(full), str(reduced), [regex('usr/bin/gone')])
    assert bad == ['usr//bin/./x', 'usr/bin/x']
    assert gone == ['usr/bin/gone']
    assert verify.deb_members(str(reduced), 'data')['usr//bin/./x'][-2] == './usr//bin/./x'


def test_invalid_unpaired_old_deb_is_not_just_missing(tmp_path):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    invalid = probe_deb(old / 'invalid-only-old.deb', data_extra=[regular('./file', b'duplicate')])
    probe_deb(old / 'sample.deb')
    probe_deb(new / 'sample.deb')
    result = cli(old, new, '--mode', 'bytes', '--json')
    assert result.returncode == 2
    assert str(invalid) in json.loads(result.stdout)['error']


def test_package_filename_difference_with_identical_contents(deb, diagnostics, tmp_path):
    old = deb(tmp_path / 'old')
    new = tmp_path / 'renamed.deb'
    shutil.copyfile(old, new)
    report, lines, status = compare.compare_packages(old, new)
    pair, = report['pairs']
    assert status == 1 and pair['different']
    assert pair['old']['sha256'] == pair['new']['sha256']
    assert not any(pair[key] for key in ('fields', 'control', 'data', 'ar'))
    assert 'filename:' in '\n'.join(lines)


def test_alternatives_symlink_from_dpkg_deb_is_metadata(deb, tmp_path):
    path = deb(tmp_path / 'debs', link='/etc/alternatives/sample')
    result = cli(path, path, '--mode', 'package', '--json')
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)['pairs'][0]['old']['data']['usr/share/sample/link']
    assert record['link'] == '/etc/alternatives/sample' and record['sha256'] is None


@pytest.mark.parametrize('change', ['extra-ar', 'compression', 'raw-name', 'order'])
def test_archive_differences_are_itemised_without_diffoscope(tmp_path, monkeypatch, change):
    def diagnostic(command, optional=False):
        return dict(status='unavailable' if optional else 'completed', error=False,
                    stderr='diffoscope is not installed' if optional else '', stdout='No differences')
    monkeypatch.setattr(compare, 'diagnostic', diagnostic)
    data = archive([regular('./file', b'content\n'), regular('./other', b'other\n')])
    old = probe_deb(tmp_path / 'old.deb', data_tar=data, compress=change == 'compression')
    kwargs = dict(data_tar=data)
    if change == 'extra-ar':
        kwargs['extra_ar'] = [('_hidden', b'notes')]
    elif change == 'raw-name':
        kwargs['data_tar'] = archive([regular('././file', b'content\n'), regular('./other', b'other\n')])
    elif change == 'order':
        kwargs['data_tar'] = archive([regular('./other', b'other\n'), regular('./file', b'content\n')])
    new = probe_deb(tmp_path / 'new.deb', **kwargs)
    report, lines, status = compare.compare_packages(old, new)
    pair, = report['pairs']
    assert status == 1 and pair['ar']
    assert 'diffoscope: unavailable' in '\n'.join(lines)
    if change == 'extra-ar':
        hidden = pair['new']['ar'][-1]
        assert hidden['name'] == '_hidden' and hidden['size'] == 5
        assert hidden['sha256'] == hashlib.sha256(b'notes').hexdigest()
        assert '_hidden' in '\n'.join(lines)
    elif change == 'raw-name':
        assert pair['data']['./file']['new']['raw_name'] == '././file'
        assert pair['data']['file']['old']['raw_name'] == './file'
    elif change == 'order':
        assert pair['data']['file']['old']['position'] == 0
        assert pair['data']['file']['new']['position'] == 1
    else:
        assert pair['old']['ar'][1]['name'] == 'control.tar.gz'
        assert pair['new']['ar'][1]['name'] == 'control.tar'


def test_unexplained_byte_difference_is_explicit(tmp_path, diagnostics, monkeypatch):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    old = probe_deb(a / 'same.deb')
    new = b / old.name
    shutil.copyfile(old, new)
    snapshot = compare.package_snapshot
    def changed_hash(path, workspace):
        result = snapshot(path, workspace)
        if path == new:
            result['sha256'] = 'f' * 64
        return result
    monkeypatch.setattr(compare, 'package_snapshot', changed_hash)
    _, lines, status = compare.compare_packages(old, new)
    assert status == 1
    assert 'no itemised difference explains' in '\n'.join(lines)


@pytest.mark.parametrize('kind', ['link', 'corrupt'])
def test_original_deb_path_in_errors(tmp_path, kind):
    if kind == 'link':
        path = probe_deb(tmp_path / 'unsafe.deb', control_extra=[('shlibs', b'', tarfile.SYMTYPE, '/outside')])
    else:
        path = tmp_path / 'broken.deb'
        path.write_bytes(b'broken')
    result = cli(path, path, '--mode', 'package')
    assert result.returncode == 2
    assert str(path) in result.stderr
    assert '/pie-compare-' not in result.stderr


def test_non_utf8_filename_returns_error_and_complete_report(deb, tmp_path):
    path = deb(tmp_path / 'new')
    old = tmp_path / 'old'
    old.mkdir()
    shutil.copyfile(path, old / os.fsdecode(b'x\xff_1_all.deb'))
    output = tmp_path / 'report'
    result = cli(old, path.parent, '--mode', 'bytes', '--out', output)
    assert result.returncode == 2 and 'Traceback' not in result.stderr
    assert 'non-UTF-8 deb filename' in result.stderr
    assert (output / 'summary.txt').read_text()
    assert json.loads((output / 'report.json').read_text())['status'] == 2


def test_diagnostic_traceback_goes_to_log_not_summary(deb, tmp_path, monkeypatch, capsys):
    old = deb(tmp_path / 'old')
    new = deb(tmp_path / 'new', payload=b'changed')
    traceback = 'Traceback\n' + 'frame details\n' * 100 + 'FinalError: failed\n'
    monkeypatch.setattr(compare, 'diagnostic', lambda *args, **kwargs:
                        dict(status='failed', error=True, returncode=2, stderr=traceback, stdout=''))
    monkeypatch.setattr(compare, 'fixed_environment', lambda: None)
    output = tmp_path / 'report'
    monkeypatch.setattr(sys, 'argv', ['pie-compare', str(old), str(new), '--mode', 'package', '--out', str(output)])
    with pytest.raises(SystemExit) as result:
        compare.main()
    assert result.value.code == 2
    summary = (output / 'summary.txt').read_text()
    assert 'Traceback' not in summary and 'frame details' not in summary
    assert 'debdiff: failed: FinalError: failed' in summary
    assert 'usr/share/sample/file' in summary
    assert (output / 'diagnostics/000-diffoscope.log').read_text() == traceback
    assert 'Traceback' not in capsys.readouterr().out


@pytest.mark.parametrize('mode', ['bytes', 'package', 'tree'])
def test_text_and_json_are_separate_formats(deb, tmp_path, mode):
    path = deb(tmp_path / 'debs')
    old = new = path if mode != 'tree' else path.parent
    text = cli(old, new, '--mode', mode)
    assert text.returncode == 0
    assert text.stdout.startswith(mode + ':')
    with pytest.raises(json.JSONDecodeError):
        json.loads(text.stdout)
    result = cli(old, new, '--mode', mode, '--json')
    assert result.returncode == 0
    assert json.loads(result.stdout)['status'] == 0


def test_tree_hashes_both_directories_in_one_batch(tmp_path, monkeypatch):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir()
    for index in range(64):
        (old / ('file-%d' % index)).write_bytes(b'raw\r\n')
    for name in ['space name', 'quote"name', 'back\\slash', 'tab\tname', 'new\nline', 'é', os.fsdecode(b'nonutf\xff')]:
        (old / name).write_text('content\n')
    (old / 'link').symlink_to('../absent')
    git('init', '--quiet', cwd=old)
    git('add', '.', cwd=old)
    expected = git('write-tree', cwd=old)
    before = (old / '.git/index').read_bytes()
    shutil.copytree(old, new, symlinks=True)
    run_git, seen = compare.git, []
    def recorded(args, **kwargs):
        if 'hash-object' in args:
            seen.append(args)
        return run_git(args, **kwargs)
    monkeypatch.setattr(compare, 'git', recorded)
    report, _, status = compare.compare_trees(str(old), str(new))
    assert status == 0 and report['old']['tree'] == expected
    assert len(seen) == 1
    assert '--stdin-paths' in seen[0] and '--no-filters' in seen[0]
    assert (old / '.git/index').read_bytes() == before
    (new / 'file-0').chmod(0o755)
    result = cli(old, new, '--mode', 'tree')
    assert result.returncode == 1
    assert 'mode change 100644 => 100755 file-0' in result.stdout


@pytest.mark.parametrize('part', ['data', 'control'])
@pytest.mark.parametrize('names', [('/', './'), ('./', '/')])
@pytest.mark.parametrize('kind', [tarfile.REGTYPE, tarfile.DIRTYPE])
def test_duplicate_inventory_keys_rejected(tmp_path, part, names, kind):
    entries = [(name, b'hidden' if kind == tarfile.REGTYPE else b'', kind, '') for name in names]
    path = probe_deb(tmp_path / 'collision.deb', **{part + '_tar': archive(entries)})
    with pytest.raises(ValueError, match='occurs twice'):
        verify.deb_members(str(path), part)
    result = invoke('compare', path, path, '--mode', 'bytes', '--json')
    assert result.returncode == 2
    assert 'occurs twice' in json.loads(result.stdout)['error']
    assert invoke('compare', path, path, '--mode', 'package').returncode == 2


def test_allowed_removal_cannot_hide_changed_root_payload(tmp_path):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    full_deb = probe_deb(old / 'same.deb', data_tar=archive([
        regular('/', b'original'), regular('./', b'same'), regular('./gone', b'gone')]))
    reduced_deb = probe_deb(new / 'same.deb', data_tar=archive([
        regular('/', b'CHANGED!'), regular('./', b'same')]))
    with pytest.raises(ValueError, match='occurs twice'):
        verify.deb_diff(str(full_deb), str(reduced_deb), [regex('gone')])
    full, reduced = [trace_output(tmp_path / label, path)
                     for label, path in [('full', full_deb), ('reduced', reduced_deb)]]
    allowed = tmp_path / 'allowed.txt'
    allowed.write_text('gone\n')
    result = invoke('verify', 'compare', full, reduced, '--allow-missing', allowed)
    assert result.returncode == 1
    assert 'occurs twice' in result.stdout and 'Traceback' not in result.stderr
