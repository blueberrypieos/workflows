# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import shlex

from conftest import TOOLS


def test_source_job_installs_python_debian():
    workflow = (TOOLS.parent / '.github/workflows/build.yml').read_text()
    source = workflow.split('\n  source:\n', 1)[1].split('\n  binary:\n', 1)[0]
    install = source.split('apt-get install', 1)[1].split('\n\n', 1)[0]
    assert 'python3-debian' in shlex.split(install)


def test_dependency_locks_are_retained_in_binary_artifact():
    workflow = (TOOLS.parent / '.github/workflows/build.yml').read_text()
    binary = workflow.split('\n  binary:\n', 1)[1]
    assert 'python3 .workflows/tools/pie-cross-record /usr/share/pietoolchain/pie-cross-deps' in binary
    assert 'python3 .workflows/tools/pie-cross-record --build-dep' in binary
    assert 'cp /build/cross-deps/base.lock /build/cross-deps/cross-deps.lock "$GITHUB_WORKSPACE/dist/.cross-deps/"' in binary
    artifact = binary.split('uses: actions/upload-artifact@', 1)[1]
    assert 'path: dist/' in artifact
    assert 'include-hidden-files: true' in artifact


def test_binary_job_checks_out_only_workflow_tools():
    workflow = (TOOLS.parent / '.github/workflows/build.yml').read_text()
    source, binary = workflow.split('\n  source:\n', 1)[1].split('\n  binary:\n', 1)
    checkouts = [step for step in binary.split('\n      - ') if 'uses: actions/checkout@' in step]
    assert len(checkouts) == 1
    checkout = checkouts[0]
    action = 'uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1'
    assert action in source and action in checkout
    for setting in ('repository: blueberrypieos/workflows', 'ref: ${{ env.WORKFLOWS_REF }}',
                    'path: .workflows', 'persist-credentials: false', 'sparse-checkout: tools/'):
        assert setting in checkout
    install = binary.split('apt-get install', 1)[1].split('\n\n', 1)[0]
    assert 'git' in shlex.split(install)
    assert binary.index(action) < binary.index('name: Build dependencies')
    assert 'raw.githubusercontent.com' not in binary
