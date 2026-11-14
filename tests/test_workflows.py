# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import shlex

from conftest import TOOLS


def test_source_job_installs_python_debian():
    workflow = (TOOLS.parent / '.github/workflows/build.yml').read_text()
    source = workflow.split('\n  source:\n', 1)[1].split('\n  binary:\n', 1)[0]
    install = source.split('apt-get install', 1)[1].split('\n\n', 1)[0]
    assert 'python3-debian' in shlex.split(install)
