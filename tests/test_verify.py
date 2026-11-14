# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import json

import pytest


def outputs(tmp_path):
    paths = [tmp_path / "full", tmp_path / "reduced"]
    for path in paths:
        (path / "debs").mkdir(parents=True)
        (path / "files.tsv").write_text("")
        (path / "outside.txt").write_text("")
        (path / "meta.json").write_text(json.dumps({"inside": "/work/trace-test"}))
    return paths


@pytest.mark.parametrize("prefix", ["/var/tmp/", "/run/"])
def test_shared_temporary_reads(tool, tmp_path, prefix, monkeypatch, capsys):
    full, reduced = outputs(tmp_path)
    for path in (full, reduced):
        (path / "debs/test.deb").write_bytes(b"deb")
    verify = tool("verify")
    monkeypatch.setitem(verify["compare"].__globals__, "deb_members", lambda *args: {})
    (reduced / "outside.txt").write_text(prefix + "stale-input\n")
    with pytest.raises(SystemExit):
        verify["compare"](str(full), str(reduced))
    assert prefix + "stale-input" in capsys.readouterr().out


def test_empty_debs_fail(tool, tmp_path, capsys):
    full, reduced = outputs(tmp_path)
    with pytest.raises(SystemExit):
        tool('verify')['compare'](str(full), str(reduced))
    assert 'no debs' in capsys.readouterr().out


@pytest.mark.parametrize('mode', ['missing', 'corrupt', 'extra', 'valid'])
def test_collection_matches_changes(tool, tmp_path, mode):
    import hashlib
    work = tmp_path / 'work'
    debs = tmp_path / 'debs'
    work.mkdir()
    debs.mkdir()
    data = b'a binary package'
    (work / 'test.changes').write_text('Format: 1.8\nChecksums-Sha256:\n %s %d test.deb\n' %
                                      (hashlib.sha256(data).hexdigest(), len(data)))
    if mode != 'missing':
        (debs / 'test.deb').write_bytes(b'bad' if mode == 'corrupt' else data)
    if mode == 'extra':
        (debs / 'other.deb').write_bytes(data)
    collect = tool('trace')['collected_debs']
    if mode == 'valid':
        assert collect(str(work), str(debs)) == ['test.deb']
    else:
        with pytest.raises(SystemExit):
            collect(str(work), str(debs))


@pytest.mark.parametrize('recorded', [(False, False), (True, False), (False, True), (True, True)])
def test_legacy_collection_check_note(tool, tmp_path, monkeypatch, capsys, recorded):
    paths = outputs(tmp_path)
    for path, has_expected in zip(paths, recorded):
        (path / 'debs/test.deb').write_bytes(b'deb')
        if has_expected:
            meta = json.loads((path / 'meta.json').read_text())
            meta['expected_debs'] = ['test.deb']
            (path / 'meta.json').write_text(json.dumps(meta))
    verify = tool('verify')
    monkeypatch.setitem(verify['compare'].__globals__, 'deb_members', lambda *args: {})
    verify['compare'](*map(str, paths))
    notes = [line for line in capsys.readouterr().out.splitlines() if 'skipping .changes' in line]
    assert notes == ['note: %s: no expected_debs metadata; skipping .changes collection check' % path
                     for path, has_expected in zip(paths, recorded) if not has_expected]
