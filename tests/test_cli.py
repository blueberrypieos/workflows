# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import importlib
import os
import shutil
import subprocess
import sys

import pytest

from conftest import TOOLS


@pytest.mark.parametrize('name,args', [('source', ['--help']), ('trace', ['--help']),
                                      ('verify', []), ('copyright', []), ('components', []), ('root', ['--help'])])
def test_dispatch_matches_entrypoint(name, args, tmp_path):
    checkout = tmp_path / 'tools'
    shutil.copytree(TOOLS, checkout, ignore=shutil.ignore_patterns('__pycache__'))
    old = subprocess.run([str(checkout / ('pie-' + name)), *args], capture_output=True, cwd=tmp_path)
    new = subprocess.run([str(checkout / 'pie'), name, *args], capture_output=True, cwd=tmp_path)
    assert (old.returncode, old.stdout, old.stderr) == (new.returncode, new.stdout, new.stderr)


def test_help_and_unknown():
    result = subprocess.run([str(TOOLS / 'pie'), '--help'], capture_output=True, text=True)
    assert result.returncode == 0
    for name in ['source', 'trace', 'prove', 'verify', 'compare', 'copyright', 'components', 'clean-matrix', 'root']:
        assert '\n  ' + name in result.stdout
    result = subprocess.run([str(TOOLS / 'pie'), 'missing'], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stderr == 'pie: unknown command: missing\n'


@pytest.mark.parametrize('module,name,args', [('prove', 'prove', ['repo', 'test', 'root']),
                                             ('clean_matrix', 'clean-matrix', ['source.dsc', 'repo', 'root'])])
def test_shell_dispatch(module, name, args, monkeypatch):
    command = importlib.import_module('pielib.commands.' + module)
    seen = []
    monkeypatch.setattr(sys, 'argv', ['pie-' + name] + args)
    monkeypatch.setattr(os, 'execv', lambda path, args: seen.append((path, args)))
    command.main()
    path = str(TOOLS / ('pie-' + name))
    assert seen == [(path, [path] + args)]
