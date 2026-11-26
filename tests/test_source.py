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


def orig_repo(tmp_path, main_left_out, panel_left_out):
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
    write('debian/changelog', 'test (1.0+ds1-1) unstable; urgency=medium\n\n  * Initial release.\n\n'
          ' -- Maxwell <maxwell@example.org>  Thu, 26 Nov 2026 08:00:00 +0000\n')
    write('debian/source/format', '3.0 (quilt)\n')
    write('debian/gbp.conf', "[DEFAULT]\nupstream-tag = upstream/%(version)s\ncomponent = ['panel']\n")
    main = ' LICENSE\n firmware.bin\n' + ('' if main_left_out else ' tools/build.sh\n')
    panel = ' COPYING\n panel.c\n' + ('' if panel_left_out else ' docs/notes\n')
    write('debian/copyright', 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n'
          'Files-Excluded: *\nFiles-Included:\n' + main + 'Files-Excluded-panel: *\nFiles-Included-panel:\n' + panel +
          '\nFiles: *\nCopyright: Test authors\nLicense: BSD-2-Clause\n')
    git('add', '.')
    git('commit', '--quiet', '-m', 'Packaging')
    return repo, env


@pytest.mark.skipif(not all(shutil.which(name) for name in ('gbp', 'mk-origtargz', 'dpkg-parsechangelog')),
                    reason='needs git-buildpackage, devscripts and dpkg-dev')
@pytest.mark.parametrize('main_left_out', [False, True])
@pytest.mark.parametrize('panel_left_out', [False, True])
def test_orig_filters_each_tarball(tmp_path, main_left_out, panel_left_out):
    repo, env = orig_repo(tmp_path, main_left_out, panel_left_out)
    out = tmp_path / 'out'
    res = subprocess.run([str(TOOLS / 'pie-source'), 'orig', '-C', str(repo), '-o', str(out)],
                         cwd=tmp_path, env=env, capture_output=True, text=True)
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
