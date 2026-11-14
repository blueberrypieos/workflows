# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import pytest


ESCAPED = [(r'file\?.c', 'file?.c', 'fileX.c'), (r'file\*.c', 'file*.c', 'fileab.c'),
           (r'file\\.c', 'file\\.c', 'file.c')]


@pytest.mark.parametrize('pattern,name,other', ESCAPED)
def test_escaped_patterns(tool, pattern, name, other):
    assert tool('copyright')['regex'](pattern).match(name)
    assert not tool('copyright')['regex'](pattern).match(other)
    assert tool('source')['dep5_matcher']([pattern])(name)
    assert not tool('source')['dep5_matcher']([pattern])(other)
    assert tool('trace')['dep5'](pattern).match(name)
    assert not tool('trace')['dep5'](pattern).match(other)


@pytest.mark.parametrize('pattern,name,other', ESCAPED)
def test_allow_missing_escaped(tool, tmp_path, monkeypatch, pattern, name, other):
    from test_verify import outputs
    full, reduced = outputs(tmp_path)
    (full / 'debs/test.deb').write_bytes(b'full')
    (reduced / 'debs/test.deb').write_bytes(b'reduced')
    allow = tmp_path / 'allow'
    allow.write_text('usr/' + pattern + '\n')
    verify = tool('verify')
    monkeypatch.setitem(verify['compare'].__globals__, 'deb_members', lambda *args: {})
    def difference(a, b, allowed):
        assert allowed[0].match('usr/' + name)
        assert not allowed[0].match('usr/' + other)
        return [], ['usr/' + name]
    monkeypatch.setitem(verify['compare'].__globals__, 'deb_diff', difference)
    verify['compare'](str(full), str(reduced), str(allow))


def test_dep5_without_site_packages():
    import subprocess
    import sys
    from conftest import TOOLS
    result = subprocess.run([sys.executable, '-I', '-S', '-c',
                             'import runpy, sys; runpy.run_path(sys.argv[1])',
                             str(TOOLS / 'pielib/dep5.py')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
