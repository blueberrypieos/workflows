# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('unshare', [False, True])
@pytest.mark.parametrize('bind', [None, ('/host slot', '/work/trace-test')])
def test_dependency_staging_mount_is_private(tool, monkeypatch, unshare, bind):
    trace = tool('trace')
    monkeypatch.setitem(trace['in_root'].__globals__, 'UNSHARE', unshare)
    root, staging = '/test root', '/host stage'
    command = trace['in_root'](root, 'resolve', bind=bind, staging=staging)
    index = command.index('-c') + 1
    script = command[index].rsplit('exec ', 1)[0] + ':'
    stubs = "mount() { printf '%s\\t' \"$@\"; printf '\\n'; }; mkdir() { :; }; "
    result = subprocess.run(['/bin/sh', '-c', stubs + script, *command[index + 1:]],
                            capture_output=True, text=True, check=True)
    mounts = [line.rstrip('\t').split('\t') for line in result.stdout.splitlines()]
    private_run = ['-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', root + '/run']
    stage_mount = ['--bind', staging, root + '/run/pie-cross']
    assert mounts.index(private_run) < mounts.index(stage_mount)
    assert mounts.count(stage_mount) == 1
    compilation = trace['in_root'](root, 'build', wrap=('strace',), bind=bind)
    assert staging not in compilation
    assert '/run/pie-cross' not in compilation[compilation.index('-c') + 1]


@pytest.mark.parametrize('compiling_side', ['F', 'R'])
def test_compilation_cannot_read_sibling_recorder(tool, monkeypatch, tmp_path, compiling_side):
    trace = tool('trace')
    namespace = trace['run_trace'].__globals__
    root = tmp_path / 'root'
    (root / 'work').mkdir(parents=True)
    dsc = tmp_path / 'test.dsc'
    dsc.write_text('source package')
    compiling = threading.Event()
    evidence_ready = threading.Event()
    observed = threading.Event()
    snapshots, stages = [], {}
    resolving_side = 'R' if compiling_side == 'F' else 'F'
    monkeypatch.setitem(namespace, 'UNSHARE', True)

    def sudo(*args, **kwargs):
        if args[:2] == ('rm', '-rf'):
            for value in args[2:]:
                path = Path(value)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        elif args[0] == 'mkdir':
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        elif args[:2] == ('cp', '-a'):
            shutil.copytree(args[2], args[3])
        elif args[0] == 'cp':
            destination = Path(args[-1])
            if destination.is_dir():
                shutil.copy(args[1], destination)
            else:
                shutil.copyfile(args[1], destination)

    def extract(command, **kwargs):
        script, bind, staging = command
        (Path(bind[0]) / 'src').mkdir()

    def external(command, **kwargs):
        script, bind, staging = command
        work = Path(bind[0])
        side = work.name[-1]
        if 'flock /work/.pie-cross-deps.lock' in script:
            stages[side] = Path(staging)
            assert not stages[side].is_relative_to(root)
            if side == resolving_side:
                assert compiling.wait(5)
            evidence = stages[side] / 'evidence'
            (evidence / 'archives').mkdir(parents=True)
            (evidence / 'status').write_text('resolver state')
            (evidence / 'receipt.json').write_text('{}')
            (evidence / 'base.lock').write_text('')
            (evidence / 'cross-deps.lock').write_text('')
            (evidence / 'archives/dependency.deb').write_bytes(b'deb')
            if side == resolving_side:
                evidence_ready.set()
                assert observed.wait(5)
        else:
            assert staging is None
            if side == compiling_side:
                compiling.set()
                assert evidence_ready.wait(5)
                assert not stages[side].exists()
                sibling_stage = stages[resolving_side]
                assert (sibling_stage / 'pie-cross-record').is_file()
                assert (sibling_stage / 'evidence/status').read_text() == 'resolver state'
                sibling = root / ('work/trace-test@' + resolving_side)
                for relative in ('pie-cross-record', 'cross-deps-evidence/status',
                                 'cross-deps-evidence/archives/dependency.deb'):
                    path = sibling / relative
                    snapshots.append(('/' + str(path.relative_to(root)), path.exists()))
                assert not list(root.rglob('pie-cross-record'))
                assert not list(root.rglob('evidence'))
                observed.set()
            (work / 'build.log').write_text('build output')
        return SimpleNamespace(returncode=0)

    monkeypatch.setitem(namespace, 'dsc_fields', lambda path: ('test', '1', []))
    monkeypatch.setitem(namespace, 'sudo', sudo)
    monkeypatch.setitem(namespace, 'sh', extract)
    monkeypatch.setitem(namespace, 'in_root', lambda root, script, *args, bind=None, staging=None, **kwargs:
                        (script, bind if bind is not None else args[1], staging))
    monkeypatch.setitem(namespace, 'tree_state', lambda path: {})
    monkeypatch.setitem(namespace, 'orig_members', lambda *args: ({}, []))
    monkeypatch.setitem(namespace, 'collected_debs', lambda *args: ['test.deb'])
    monkeypatch.setattr(namespace['subprocess'], 'run', external)
    with ThreadPoolExecutor(2) as pool:
        tasks = [pool.submit(trace['run_trace'], str(root), str(dsc), str(tmp_path / side),
                             1, True, 'test', slot=side, cross_deps=True) for side in ('F', 'R')]
        for task in tasks:
            task.result(timeout=10)
    assert len(snapshots) == 3 and all(not present for path, present in snapshots)
    assert stages['F'] != stages['R']
    for side in ('F', 'R'):
        assert not stages[side].exists()
        assert (tmp_path / side / 'cross-deps/archives/dependency.deb').read_bytes() == b'deb'
