# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import subprocess
from pathlib import Path
import tempfile

import pytest

from conftest import TOOLS


@pytest.mark.parametrize('kind', ['outside', 'symlink', 'occupied'])
def test_matrix_runner_rejects_unsafe_workdir(kind):
    with tempfile.TemporaryDirectory(prefix='pie-matrix-', dir='/tmp') as directory:
        work = Path(directory)
        if kind == 'outside':
            argument = Path("/pie-matrix-outside")
        elif kind == 'symlink':
            argument = work / 'link'
            argument.symlink_to(work)
        else:
            argument = work
            (work / 'f1').mkdir()
        entries = set(work.iterdir())
        result = subprocess.run([str(TOOLS / 'pie-clean-matrix-cases'), str(argument)],
                                capture_output=True, timeout=10)
        assert result.returncode == 2
        assert b'usage: pie-clean-matrix-cases' in result.stderr
        assert set(work.iterdir()) == entries
        if kind == 'outside':
            assert b'not under /tmp' in result.stderr
    assert not work.exists()


def test_existing_matrix_cases(request):
    dsc = request.config.getoption('--clean-matrix-dsc')
    if not dsc:
        pytest.skip('pass --clean-matrix-dsc DSC on a host with the SDK to run cases f to h')
    assert Path('/usr/share/pietoolchain/rules.mk').is_file(), 'install the SDK before running matrix cases'
    import shutil
    with tempfile.TemporaryDirectory(prefix='pie-matrix-', dir='/tmp') as directory:
        for path in Path(dsc).resolve().parent.iterdir():
            if path.is_file():
                shutil.copy2(path, directory)
        result = subprocess.run([str(TOOLS / 'pie-clean-matrix-cases'), directory], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
