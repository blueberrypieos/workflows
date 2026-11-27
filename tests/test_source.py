# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os
import shutil
import subprocess
import sys
import tarfile

import pytest

from conftest import TOOLS


def test_binary_fixed_environment(tool, monkeypatch):
    source = tool('source')
    seen = {}
    monkeypatch.setattr(os, 'environ', dict(os.environ, HOME='/caller', DEB_BUILD_OPTIONS='nocheck',
                                         TAR_OPTIONS='--broken', PIE_QUIET='1'))
    monkeypatch.setattr(sys, 'argv', ['pie-source', 'binary', 'test.dsc'])
    monkeypatch.setitem(source['main'].__globals__, 'build_binary', lambda *args: seen.update(os.environ))
    source['main']()
    assert seen['HOME'] == '/nonexistent'
    assert seen['LC_ALL'] == 'C.UTF-8'
    assert seen['PIE_QUIET'] == '1'
    assert 'DEB_BUILD_OPTIONS' not in seen
    assert 'TAR_OPTIONS' not in seen


def orig_repo(tmp_path, main_left_out=False, panel_left_out=False, version='1.0+ds1', comps=('panel',), header=None):
    home = tmp_path / 'home'
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               GIT_AUTHOR_NAME='Maxwell', GIT_AUTHOR_EMAIL='Maxwell',
               GIT_COMMITTER_NAME='Maxwell', GIT_COMMITTER_EMAIL='Maxwell')

    def git(*args):
        subprocess.run(['git', *args], cwd=repo, env=env, check=True, capture_output=True)

    def write(name, text):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)

    repo = tmp_path / 'repo'
    repo.mkdir()
    git('init', '--quiet', '--initial-branch=main')
    for name in ['LICENSE', 'firmware.bin', 'tools/build.sh', 'panel/COPYING', 'panel/panel.c', 'panel/docs/notes']:
        write(name, name + '\n')
    git('add', '.')
    git('commit', '--quiet', '-m', 'New upstream version 1.0')
    git('tag', 'upstream/1.0')
    write('debian/changelog', 'test (%s-1) unstable; urgency=medium\n\n  * Initial release.\n\n'
          ' -- Maxwell <maxwell@example.org>  Thu, 26 Nov 2026 08:00:00 +0000\n' % version)
    write('debian/source/format', '3.0 (quilt)\n')
    write('debian/gbp.conf', "[DEFAULT]\nupstream-tag = upstream/%%(version)s\ncomponent = %r\n" % list(comps))
    if header is None:
        main = ' LICENSE\n firmware.bin\n' + ('' if main_left_out else ' tools/build.sh\n')
        panel = ' COPYING\n panel.c\n' + ('' if panel_left_out else ' docs/notes\n')
        header = 'Files-Excluded: *\nFiles-Included:\n' + main + 'Files-Excluded-panel: *\nFiles-Included-panel:\n' + panel
    write('debian/copyright', 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n' + header +
          '\nFiles: *\nCopyright: Test authors\nLicense: BSD-2-Clause\n')
    git('add', '.')
    git('commit', '--quiet', '-m', 'Packaging')
    return repo, env


needs_orig_tools = pytest.mark.skipif(
    not all(shutil.which(name) for name in ('gbp', 'mk-origtargz', 'dpkg-parsechangelog')),
    reason='needs git-buildpackage, devscripts and dpkg-dev')


def pie_orig(tmp_path, repo, env, *args):
    return subprocess.run([str(TOOLS / 'pie-source'), 'orig', *args, '-C', str(repo), '-o', str(tmp_path / 'out')],
                          cwd=tmp_path, env=env, capture_output=True, text=True)


@needs_orig_tools
@pytest.mark.parametrize('main_left_out', [False, True])
@pytest.mark.parametrize('panel_left_out', [False, True])
def test_orig_filters_each_tarball(tmp_path, main_left_out, panel_left_out):
    repo, env = orig_repo(tmp_path, main_left_out, panel_left_out)
    out = tmp_path / 'out'
    res = pie_orig(tmp_path, repo, env)
    assert res.returncode == 0, res.stderr
    main, panel = out / 'test_1.0+ds1.orig.tar.xz', out / 'test_1.0+ds1.orig-panel.tar.xz'
    assert res.stdout.split() == [str(main), str(panel)]

    def files(tarball):
        assert tarball.read_bytes()[:6] == b'\xfd7zXZ\x00'
        with tarfile.open(tarball, 'r:xz') as t:
            return sorted(m.name for m in t.getmembers() if m.isfile())

    assert files(main) == ['test-1.0+ds1/LICENSE', 'test-1.0+ds1/firmware.bin'] + \
        ([] if main_left_out else ['test-1.0+ds1/tools/build.sh'])
    assert files(panel) == ['panel/COPYING'] + ([] if panel_left_out else ['panel/docs/notes']) + ['panel/panel.c']


MAIN_FIELDS = 'Files-Excluded: *\nFiles-Included:\n LICENSE\n firmware.bin\n'
PANEL_FIELDS = 'Files-Excluded-panel: *\nFiles-Included-panel:\n COPYING\n panel.c\n'


@needs_orig_tools
@pytest.mark.parametrize('version, comps, header, error', [
    ('1.0', ('panel',), PANEL_FIELDS, 'leaves files out, but has no Files-Excluded field'),
    ('1.0+ds1', ('panel',), PANEL_FIELDS, 'leaves files out, but has no Files-Excluded field'),
    ('1.0+ds1', ('panel',), MAIN_FIELDS, 'leaves files out, but has no Files-Excluded-panel field'),
    ('1.0', ('panel',), '', None),
    ('1.0', ('panel',), MAIN_FIELDS + PANEL_FIELDS, 'the version needs a +dsN suffix'),
    ('1.0+ds1', ('panel',), '', 'is a +ds version'),
    ('1.0', (), PANEL_FIELDS, 'Files-Excluded-panel names no component'),
    ('1.0', ('panel',), 'Files-Included-panel:\n panel.c\n', 'Files-Included-panel needs Files-Excluded-panel'),
    ('1.0', ('panel',), 'Files-Included:\n LICENSE\n', 'Files-Included needs Files-Excluded'),
    ('1.0', ('panel',), PANEL_FIELDS.lower(), 'leaves files out, but has no Files-Excluded field'),
    ('1.0', (), PANEL_FIELDS.lower(), 'files-excluded-panel names no component'),
    ('1.0', ('panel',), 'files-included-panel:\n panel.c\n', 'files-included-panel needs Files-Excluded-panel'),
    ('1.0+ds1', ('panel',), MAIN_FIELDS + PANEL_FIELDS.replace('Included-panel', 'Included-PANEL'), None),
    ('1.0+ds1', ('panel',), MAIN_FIELDS + PANEL_FIELDS.replace('-panel', '-Panel'), None),
    ('1.0+ds1', ('panel',), 'Files-Excluded:\n' + PANEL_FIELDS, None),
    ('1.0+ds1', ('panel',), MAIN_FIELDS.replace('Files-', 'files-') + PANEL_FIELDS, None),
], ids=['component-only', 'component-only-ds', 'main-only-ds', 'none', 'no-ds-suffix', 'ds-unfiltered',
        'unknown-component', 'included-only-component', 'included-only-main', 'lowercase-component-only',
        'lowercase-unknown-component', 'lowercase-included-only', 'uppercase-component', 'mixed-case-component',
        'empty-main-exclusions', 'lowercase-main'])
def test_orig_fields_of_each_tarball(tmp_path, version, comps, header, error):
    repo, env = orig_repo(tmp_path, version=version, comps=comps, header=header)
    res = pie_orig(tmp_path, repo, env)
    out = tmp_path / 'out'
    if error:
        assert res.returncode != 0 and error in res.stderr, res.stderr
        assert not out.exists() or not list(out.iterdir())
        return
    assert res.returncode == 0, res.stderr
    with tarfile.open(out / ('test_%s.orig.tar.xz' % version), 'r:xz') as t:
        assert sorted(m.name for m in t.getmembers() if m.isfile()) == \
            ['test-%s/LICENSE' % version, 'test-%s/firmware.bin' % version] + \
            ([] if 'files-included:\n' in header.lower() else ['test-%s/tools/build.sh' % version])
    with tarfile.open(out / ('test_%s.orig-panel.tar.xz' % version), 'r:xz') as t:
        assert sorted(m.name for m in t.getmembers() if m.isfile()) == \
            ['panel/COPYING'] + ([] if header else ['panel/docs/notes']) + ['panel/panel.c']


@needs_orig_tools
@pytest.mark.parametrize('comps, header', [
    (('panel',), PANEL_FIELDS),
    (('panel',), 'Files-Included-panel:\n panel.c\n'),
    ((), PANEL_FIELDS),
], ids=['component-only', 'included-only', 'unknown-component'])
def test_orig_full_keeps_every_file(tmp_path, comps, header):
    repo, env = orig_repo(tmp_path, version='1.0', comps=comps, header=header)
    res = pie_orig(tmp_path, repo, env, '--full')
    assert res.returncode == 0, res.stderr
    out = tmp_path / 'out'
    with tarfile.open(out / 'test_1.0.orig.tar.xz', 'r:xz') as t:
        names = sorted(m.name for m in t.getmembers() if m.isfile())
    if comps:
        assert names == ['test-1.0/LICENSE', 'test-1.0/firmware.bin', 'test-1.0/tools/build.sh']
        with tarfile.open(out / 'test_1.0.orig-panel.tar.xz', 'r:xz') as t:
            assert sorted(m.name for m in t.getmembers() if m.isfile()) == \
                ['panel/COPYING', 'panel/docs/notes', 'panel/panel.c']
    else:
        assert len(names) == 6
