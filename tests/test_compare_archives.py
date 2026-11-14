# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tarfile

import pytest

from conftest import TOOLS
from pielib.commands import compare
from pielib.deb import archive_inventory


def regular(name, body):
    return name, body, tarfile.REGTYPE, ''


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for name, body, kind, link, *attributes in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o644
            member.linkname = link
            member.size = len(body) if kind == tarfile.REGTYPE else 0
            for key, value in (attributes[0] if attributes else {}).items():
                setattr(member, key, value)
            tar.addfile(member, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
    return stream.getvalue()


def probe_deb(path, version='1', control_extra=(), data_extra=(), payload=b'content\n', compress=False,
              raw_control=None, control_tar=None, data_tar=None, extra_ar=()):
    control = ('Package: sample\nVersion: %s\nArchitecture: all\n'
               'Maintainer: Example <example@example.org>\nDescription: sample\n' % version).encode()
    parts = [('debian-binary', b'2.0\n'),
             ('control.tar', archive([regular('./control', control if raw_control is None else raw_control), *control_extra])),
             ('data.tar', archive([regular('./file', payload), *data_extra]))]
    if control_tar is not None:
        parts[1] = ('control.tar', control_tar)
    if data_tar is not None:
        parts[2] = ('data.tar', data_tar)
    if compress:
        parts = [(name + '.gz', gzip.compress(body, mtime=0)) if name.endswith('.tar') else (name, body)
                 for name, body in parts]
    parts.extend(extra_ar)
    with path.open('wb') as output:
        output.write(b'!<arch>\n')
        for name, body in parts:
            header = '%-16s%-12s%-6s%-6s%-8s%-10s`\n' % (name + '/', 0, 0, 0, '100644', len(body))
            output.write(header.encode('ascii'))
            output.write(body)
            if len(body) % 2:
                output.write(b'\n')
    return path


def invoke(*args, **kwargs):
    return subprocess.run([str(TOOLS / 'pie'), *map(str, args)], capture_output=True, text=True, **kwargs)


def trace_output(path, deb):
    (path / 'debs').mkdir(parents=True)
    shutil.copyfile(deb, path / 'debs' / deb.name)
    (path / 'meta.json').write_text(json.dumps(dict(inside='/work/probe', expected_debs=[deb.name])))
    (path / 'files.tsv').write_text('')
    (path / 'outside.txt').write_text('')
    return path


@pytest.mark.parametrize('part', ['data', 'control'])
@pytest.mark.parametrize('kind', ['duplicate', 'alias', 'dangling'])
def test_identical_invalid_debs_fail_bytes_and_verify(tmp_path, part, kind):
    name = './file' if part == 'data' else './control'
    entry = (regular(name if kind == 'duplicate' else '././' + name, b'duplicate\n') if kind != 'dangling'
             else ('hardlink', b'', tarfile.LNKTYPE, 'absent'))
    deb = probe_deb(tmp_path / 'invalid.deb', **{part + '_extra': [entry]})
    output = tmp_path / 'report'
    result = invoke('compare', deb, deb, '--mode', 'bytes', '--out', output)
    assert result.returncode == 2
    assert 'Traceback' not in result.stderr
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 2
    text = 'twice' if kind != 'dangling' else 'not a regular file before it'
    assert text in report['error']
    old, new = [trace_output(tmp_path / label, deb) for label in ('full', 'reduced')]
    verification = invoke('verify', 'compare', old, new)
    assert verification.returncode == 1
    assert text in verification.stdout
    assert invoke('compare', deb, deb, '--mode', 'package').returncode == 2


def test_control_symlink_preserves_external_sentinel(tmp_path):
    sentinel = tmp_path / 'outside-sentinel'
    sentinel.write_text('z line\na line\n')
    sentinel.chmod(0o600)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    extra = [('shlibs', b'', tarfile.SYMTYPE, str(sentinel))]
    a = probe_deb(tmp_path / 'old.deb', control_extra=extra)
    b = probe_deb(tmp_path / 'new.deb', version='2', control_extra=extra)
    output = tmp_path / 'report'
    result = invoke('compare', a, b, '--mode', 'package', '--out', output,
                    env=dict(os.environ, TMPDIR=str(scratch)))
    assert result.returncode == 2
    assert 'unsafe control member' in result.stderr
    assert sentinel.read_text() == 'z line\na line\n'
    assert sentinel.stat().st_mode & 0o777 == 0o600
    assert list(scratch.iterdir()) == []
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 2 and 'unsafe control member' in report['error']


@pytest.mark.parametrize('part', ['control', 'data'])
@pytest.mark.parametrize('kind', ['absolute-path', 'parent-path', 'absolute-symlink', 'parent-symlink',
                                 'absolute-hardlink', 'parent-hardlink', 'symlink-parent', 'fifo',
                                 'resolved-escape', 'forward-resolved-escape', 'link-cycle'])
def test_control_safety_and_data_metadata(tmp_path, monkeypatch, part, kind):
    sentinel = tmp_path / 'outside-sentinel'
    sentinel.write_text('unchanged\n')
    sentinel.chmod(0o600)
    entries = {
        'absolute-path': [regular(str(sentinel), b'changed\n')],
        'parent-path': [regular('../outside-sentinel', b'changed\n')],
        'absolute-symlink': [('shlibs', b'', tarfile.SYMTYPE, str(sentinel))],
        'parent-symlink': [('nested/link', b'', tarfile.SYMTYPE, '../../outside-sentinel')],
        'absolute-hardlink': [('shlibs', b'', tarfile.LNKTYPE, str(sentinel))],
        'parent-hardlink': [('shlibs', b'', tarfile.LNKTYPE, '../outside-sentinel')],
        'symlink-parent': [('dir', b'', tarfile.SYMTYPE, '.'), regular('dir/child', b'changed\n')],
        'fifo': [('fifo', b'', tarfile.FIFOTYPE, '')],
        'resolved-escape': [('nested/up', b'', tarfile.SYMTYPE, '..'),
                            ('shlibs', b'', tarfile.SYMTYPE, 'nested/up/../outside-sentinel')],
        'forward-resolved-escape': [('shlibs', b'', tarfile.SYMTYPE, 'nested/up/../outside-sentinel'),
                                    ('nested/up', b'', tarfile.SYMTYPE, '..')],
        'link-cycle': [('shlibs', b'', tarfile.SYMTYPE, 'shlibs')],
    }[kind]
    a = probe_deb(tmp_path / 'old.deb', **{part + '_extra': entries})
    b = probe_deb(tmp_path / 'new.deb', version='2')
    def unexpected(*args, **kwargs):
        pytest.fail('unsafe archive reached a diagnostic tool')
    monkeypatch.setattr(compare, 'diagnostic', unexpected)
    if part == 'data' and kind not in ('absolute-hardlink', 'parent-hardlink'):
        report, _, status = compare.compare_packages(a, a)
        assert status == 0
        assert report['pairs'][0]['old']['data']
    else:
        with pytest.raises((ValueError, OSError)):
            compare.compare_packages(a, b)
    assert sentinel.read_text() == 'unchanged\n'
    assert sentinel.stat().st_mode & 0o777 == 0o600


def test_contained_links_and_streamed_metadata(tmp_path):
    deb = probe_deb(tmp_path / 'links.deb', data_extra=[
        ('hardlink', b'', tarfile.LNKTYPE, './file'),
        ('nested/link', b'', tarfile.SYMTYPE, '../file')])
    root = tmp_path / 'unpacked'
    records, raw = archive_inventory(deb, 'data', root)
    assert raw is None
    assert records['file']['sha256'] == records['hardlink']['sha256']
    assert records['file']['size'] == records['hardlink']['size'] == len(b'content\n')
    assert not root.exists()
    assert records['nested/link']['link'] == '../file'


def test_inventory_preserves_owners_and_extended_headers(tmp_path):
    deb = probe_deb(tmp_path / 'metadata.deb', data_extra=[
        ('./owned', b'contents', tarfile.REGTYPE, '',
         dict(mode=0o640, uid=123, gid=456, uname='user', gname='group', pax_headers={'key': 'value'}))])
    records, _ = archive_inventory(deb, 'data', tmp_path / 'unpacked')
    record = records['owned']
    assert (record['mode'], record['uid'], record['gid'], record['uname'], record['gname']) == (
        '0640', 123, 456, 'user', 'group')
    assert record['pax_headers'] == {'key': 'value'}


def test_raw_control_keeps_original_line_endings(tmp_path):
    raw = (b'Package: sample\r\nVersion: 1\r\nArchitecture: all\r\n'
           b'Maintainer: Example <example@example.org>\r\nDescription: sample\r\n')
    deb = probe_deb(tmp_path / 'line-endings.deb', raw_control=raw)
    output = tmp_path / 'report'
    result = invoke('compare', deb, deb, '--mode', 'package', '--out', output)
    assert result.returncode == 0, result.stderr
    snapshot = json.loads((output / 'report.json').read_text())['pairs'][0]['old']
    assert snapshot['raw_control'] == raw.decode()
    assert snapshot['control']['control']['sha256'] == hashlib.sha256(raw).hexdigest()


def test_diagnostics_read_validated_deb_copies(tmp_path, monkeypatch):
    a = probe_deb(tmp_path / 'old.deb')
    b = probe_deb(tmp_path / 'new.deb', version='2')
    expected = {p.name: p.read_bytes() for p in (a, b)}
    seen = []
    def diagnostic(command, optional=False):
        a.write_bytes(b'replaced after validation')
        for argument in command[-2:]:
            path = Path(argument)
            assert path not in (a, b)
            assert path.is_file() and path.suffix == '.deb'
            assert path.read_bytes() == expected[path.name]
            seen.append(path)
        return dict(status='completed', error=False)
    monkeypatch.setattr(compare, 'diagnostic', diagnostic)
    assert compare.compare_packages(a, b)[2] == 1
    assert len(seen) == 4
    assert all(not path.exists() for path in seen)


@pytest.mark.skipif(not all(shutil.which(name) for name in ('diffoscope', 'debdiff')),
                    reason='install debdiff and diffoscope for content diagnostics integration')
def test_compressed_stale_md5sums_cannot_hide_differing_files(tmp_path):
    checksum = hashlib.md5(b'content\n').hexdigest().encode() + b'  file\n'
    extra = [regular('./md5sums', checksum)]
    a = probe_deb(tmp_path / 'old.deb', control_extra=extra,
                  data_extra=[regular('./other', b'first\n')], compress=True)
    b = probe_deb(tmp_path / 'new.deb', control_extra=extra, payload=b'altered\n',
                  data_extra=[regular('./other', b'later\n')], compress=True)
    output = tmp_path / 'report'
    result = invoke('compare', a, b, '--mode', 'package', '--out', output)
    assert result.returncode == 1, result.stderr + result.stdout
    pair, = json.loads((output / 'report.json').read_text())['pairs']
    assert set(pair['data']) == {'file', 'other'}
    assert pair['control'] == {}
    details = pair['diagnostics']['diffoscope']
    assert '--force-details' in details['command']
    assert details['status'] == 'completed'
    assert './file' in details['stdout'] and './other' in details['stdout']
    for text in ('-content', '+altered', '-first', '+later'):
        assert text in details['stdout']


@pytest.fixture
def large_deb(tmp_path):
    root = tmp_path / 'root'
    (root / 'DEBIAN').mkdir(parents=True)
    (root / 'DEBIAN/control').write_text(
        'Package: large\nVersion: 1\nArchitecture: all\n'
        'Maintainer: Example <example@example.org>\nDescription: large\n')
    with (root / 'payload').open('wb') as stream:
        for _ in range(64):
            stream.write(b'x' * 1024 * 1024)
    path = tmp_path / 'large.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '-Zgzip', '-b', str(root), str(path)],
                   check=True, capture_output=True)
    return path


@pytest.mark.parametrize('mode', ['bytes', 'package'])
def test_64_mib_payload_fits_160_mib_address_limit(large_deb, tmp_path, mode):
    output = tmp_path / 'report'
    def limit():
        resource.setrlimit(resource.RLIMIT_AS, (160 * 1024 * 1024, 160 * 1024 * 1024))
    result = invoke('compare', large_deb, large_deb, '--mode', mode, '--out', output, preexec_fn=limit)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 0
    if mode == 'package':
        entry = report['pairs'][0]['old']['data']['payload']
        assert entry['size'] == 64 * 1024 * 1024
        digest = hashlib.sha256()
        for _ in range(64):
            digest.update(b'x' * 1024 * 1024)
        assert entry['sha256'] == digest.hexdigest()


def test_memory_failure_is_an_error_with_a_report(tmp_path, monkeypatch, capsys):
    def fail(*args):
        raise MemoryError
    output = tmp_path / 'report'
    monkeypatch.setattr(compare, 'compare_packages', fail)
    monkeypatch.setattr(compare, 'fixed_environment', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['pie-compare', 'old', 'new', '--mode', 'package', '--out', str(output)])
    with pytest.raises(SystemExit) as error:
        compare.main()
    assert error.value.code == 2
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 2 and 'resource failure' in report['error']
    assert 'Traceback' not in capsys.readouterr().err
