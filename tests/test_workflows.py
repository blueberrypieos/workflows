# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os
import shlex
import subprocess

import pytest

from conftest import TOOLS


def test_source_job_installs_python_debian():
    workflow = (TOOLS.parent / '.github/workflows/build.yml').read_text()
    source = workflow.split('\n  source:\n', 1)[1].split('\n  binary:\n', 1)[0]
    install = source.split('apt-get install', 1)[1].split('\n\n', 1)[0]
    assert 'python3-debian' in shlex.split(install)


def source_submodule_command():
    workflow = (TOOLS.parent / '.github/workflows/build.yml').read_text()
    source = workflow.split('\n  source:\n', 1)[1].split('\n  binary:\n', 1)[0]
    checkout = next(step for step in source.split('\n      - ')
                    if 'uses: actions/checkout@' in step and 'repository:' not in step)
    submodules = next(step for step in source.split('\n      - ') if step.startswith('name: Submodules\n'))
    assert 'fetch-depth: 0' in checkout
    assert 'submodules:' not in checkout
    assert source.index(checkout) < source.index(submodules) < source.index('name: Source package')
    return shlex.split(submodules.split('run:', 1)[1].strip())


@pytest.mark.parametrize('with_submodules', [False, True])
def test_source_submodules_are_shallow_pinned_and_exported(tool, tmp_path, with_submodules):
    env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0',
               GIT_AUTHOR_NAME='Maxwell', GIT_AUTHOR_EMAIL='Maxwell',
               GIT_COMMITTER_NAME='Maxwell', GIT_COMMITTER_EMAIL='Maxwell')

    def git(repo, *args):
        return subprocess.run(['git', *args], cwd=repo, env=env, check=True,
                              text=True, capture_output=True).stdout.strip()

    def init(name):
        repo = tmp_path / name
        repo.mkdir()
        git(repo, 'init', '--quiet', '--initial-branch=main')
        return repo

    def commit(repo, name, text):
        (repo / name).write_text(text)
        git(repo, 'add', '.')
        git(repo, 'commit', '--quiet', '-m', 'Update source')
        return git(repo, 'rev-parse', 'HEAD')

    project = init('project')
    (project / 'debian').mkdir()
    (project / 'debian/copyright').write_text(
        'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n\n'
        'Files: *\nCopyright: Test authors\nLicense: BSD-2-Clause\n')
    commit(project, 'README', 'Project source\n')
    if with_submodules:
        nested = init('nested')
        commit(nested, 'nested.h', 'Old nested source\n')
        nested_pin = commit(nested, 'nested.h', 'Pinned nested source\n')
        dependency = init('dependency')
        commit(dependency, 'required.c', 'Old source\n')
        git(dependency, 'submodule', 'add', '--quiet', nested.as_uri(), 'nested')
        dependency_pin = commit(dependency, 'required.c', 'Pinned source\n')
        git(project, 'submodule', 'add', '--quiet', dependency.as_uri(), 'src/dependency')
        git(project, 'commit', '--quiet', '-am', 'Pin dependencies')
        commit(nested, 'nested.h', 'Newer unpinned nested source\n')
        commit(dependency, 'required.c', 'Newer unpinned source\n')

    checkout = tmp_path / 'checkout'
    git(tmp_path, 'clone', '--quiet', project.as_uri(), str(checkout))
    if with_submodules:
        assert not (checkout / 'src/dependency/.git').exists()
    subprocess.run(source_submodule_command(), cwd=checkout, env=env,
                   check=True, text=True, capture_output=True)

    if with_submodules:
        for path, pin in [('src/dependency', dependency_pin), ('src/dependency/nested', nested_pin)]:
            assert git(checkout / path, 'rev-parse', 'HEAD') == pin
            assert git(checkout / path, 'rev-parse', '--is-shallow-repository') == 'true'
            assert git(checkout / path, 'rev-list', '--count', 'HEAD') == '1'
    exported = tmp_path / 'source'
    tool('source')['assemble'](str(checkout), str(exported), False)
    assert (exported / 'README').read_text() == 'Project source\n'
    if with_submodules:
        assert (exported / 'src/dependency/required.c').read_text() == 'Pinned source\n'
        assert (exported / 'src/dependency/nested/nested.h').read_text() == 'Pinned nested source\n'


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
