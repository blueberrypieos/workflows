# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

from conftest import TOOLS


RESOLVER = '''import os, pathlib, shutil, sys, tempfile

def main():
    tmp = tempfile.mkdtemp()
    os.chmod(tmp, 0o755)
    try:
        source = pathlib.Path(sys.argv[1])
        shutil.copyfile(source / 'status', pathlib.Path(tmp, 'status'))
        for archive in source.glob('*.deb'):
            shutil.copyfile(archive, pathlib.Path(tmp, archive.name))
        old = source / 'old-cross-root'
        old.mkdir()
        shutil.rmtree(old)
    finally:
        shutil.rmtree(tmp)
'''


@pytest.fixture
def archive(tmp_path):
    tree = tmp_path / 'package'
    (tree / 'DEBIAN').mkdir(parents=True)
    (tree / 'DEBIAN/control').write_text('Package: example\nVersion: 1.2\nArchitecture: darwin-arm64\n'
                                       'Maintainer: Test <test@invalid>\nDescription: Test package\n')
    (tree / 'payload').write_bytes(b'build dependency')
    path = tmp_path / 'example.deb'
    subprocess.run(['dpkg-deb', '--build', str(tree), str(path)], check=True, capture_output=True)
    return path


def test_retains_resolver_state_and_hashes_actual_download(archive, tmp_path):
    helper = tmp_path / 'resolver.py'
    helper.write_text(RESOLVER)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'status').write_text('Package: pietoolchain-sdk\nVersion: 0.2.1\nArchitecture: all\n'
                                 'Status: install ok installed\nProvides: example-abi\n\n')
    shutil.copyfile(archive, source / archive.name)
    evidence = tmp_path / 'evidence'
    subprocess.run([sys.executable, '-I', str(TOOLS / 'pie-cross-record'), str(helper), str(source),
                    str(evidence)], check=True)
    capture = json.loads((evidence / 'receipt.json').read_text())
    assert (evidence / 'status').read_bytes() == (source / 'status').read_bytes()
    assert capture['status_sha256'] == hashlib.sha256((source / 'status').read_bytes()).hexdigest()
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert capture['packages'] == [{'package': 'example', 'version': '1.2', 'architecture': 'darwin-arm64',
                                    'sha256': sha, 'archive': sha + '.deb', 'origin': 'download'}]
    assert (evidence / 'archives' / (sha + '.deb')).read_bytes() == archive.read_bytes()
    assert not (source / 'old-cross-root').exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['evidence', 'example.deb', 'package', 'resolver.py', 'source']


@pytest.mark.parametrize('available', [True, False])
def test_installed_provider_uses_build_status_and_cached_archive(archive, tmp_path, available):
    namespace = runpy.run_path(str(TOOLS / 'pie-cross-record'))
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'status').write_text('Package: example\nVersion: 1.2\nArchitecture: darwin-arm64\n'
                                'Status: install ok installed\n\n')
    cache = tmp_path / 'cache'
    cache.mkdir()
    if available:
        shutil.copyfile(archive, cache / archive.name)
        namespace['capture'](state, tmp_path / 'evidence', cache)
        capture = json.loads((tmp_path / 'evidence/receipt.json').read_text())
        assert capture['packages'][0]['origin'] == 'installed'
        assert capture['packages'][0]['sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    else:
        with pytest.raises(ValueError, match='unique cached archive'):
            namespace['capture'](state, tmp_path / 'evidence', cache)

