# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('unshare', [False, True])
@pytest.mark.parametrize('bind', [None, ('/host slot', '/work/trace-test')])
@pytest.mark.parametrize('wrap', [(), ('strace',)])
def test_private_temporary_mounts(tool, monkeypatch, unshare, bind, wrap):
    trace = tool('trace')
    monkeypatch.setitem(trace['in_root'].__globals__, 'UNSHARE', unshare)
    root = '/test root'
    command = trace['in_root'](root, 'unused build', wrap, bind)
    script_index = command.index('-c') + 1
    script = command[script_index].rsplit('exec ', 1)[0] + ':'
    stubs = "mount() { printf '%s\\t' \"$@\"; printf '\\n'; }; mkdir() { :; }; "
    result = subprocess.run(['/bin/sh', '-c', stubs + script, *command[script_index + 1:]],
                            capture_output=True, text=True, check=True)
    mounts = [line.rstrip('\t').split('\t') for line in result.stdout.splitlines()]
    private = [args for args in mounts if args[:2] == ['-t', 'tmpfs']]
    assert private == [['-t', 'tmpfs', '-o', 'mode=' + mode, 'tmpfs', root + '/' + path]
                       for path, mode in [('tmp', '1777'), ('var/tmp', '1777'), ('run', '755')]]
    if bind:
        assert ['--bind', bind[0], root + bind[1]] in mounts


@pytest.mark.parametrize('build_rc', [0, 31])
def test_trace_records_private_mounts(tool, monkeypatch, tmp_path, build_rc):
    import json
    from types import SimpleNamespace
    trace = tool('trace')
    namespace = trace['run_trace'].__globals__
    root = tmp_path / 'root'
    work = root / 'work/trace-test'
    (work / 'src').mkdir(parents=True)
    (work / 'build.log').write_text('build output\n')
    (work / 'test.deb').write_bytes(b'deb')
    dsc = tmp_path / 'test.dsc'
    dsc.write_text('source package')
    out = tmp_path / 'output'
    monkeypatch.setitem(namespace, 'dsc_fields', lambda path: ('test', '1', []))
    monkeypatch.setitem(namespace, 'sudo', lambda *args, **kw: None)
    monkeypatch.setitem(namespace, 'sh', lambda *args, **kw: None)
    monkeypatch.setattr(namespace['subprocess'], 'run', lambda *args, **kw: SimpleNamespace(returncode=build_rc))
    monkeypatch.setitem(namespace, 'tree_state', lambda path: {})
    monkeypatch.setitem(namespace, 'orig_members', lambda *args: ({}, []))
    monkeypatch.setitem(namespace, 'collected_debs', lambda *args: ['test.deb'])
    if build_rc:
        with pytest.raises(SystemExit, match='the build failed'):
            trace['run_trace'](str(root), str(dsc), str(out), 1, True, 'test')
    else:
        trace['run_trace'](str(root), str(dsc), str(out), 1, True, 'test')
    metadata = json.loads((out / 'meta.json').read_text())
    if build_rc:
        assert not metadata.get('private_tmpfs')
    else:
        assert metadata['private_tmpfs'] == {'/tmp': '1777', '/var/tmp': '1777', '/run': '755'}


@pytest.mark.parametrize('unshare', [False, True])
@pytest.mark.parametrize('different', [False, True])
@pytest.mark.parametrize('native_reference', [False, True])
def test_dependency_capture_precedes_compilation_under_lock(tool, monkeypatch, tmp_path, unshare, different, native_reference):
    import hashlib
    import json
    import shutil
    from types import SimpleNamespace
    trace = tool('trace')
    namespace = trace['run_trace'].__globals__
    monkeypatch.setitem(namespace, 'UNSHARE', unshare)
    root = tmp_path / 'root'
    work = root / 'work/trace-test'
    (work / 'src').mkdir(parents=True)
    (work / 'build.log').write_text('build output\n')
    dsc = tmp_path / 'test.dsc'
    dsc.write_text('source package')
    out = tmp_path / 'output'
    expected = tmp_path / 'expected.lock'
    native_record = ''
    if native_reference:
        archive = b'native archive'
        digest = hashlib.sha256(archive).hexdigest()
        (tmp_path / 'cross-deps/archives').mkdir(parents=True)
        (tmp_path / 'cross-deps/archives' / (digest + '.deb')).write_bytes(archive)
        native_record = 'build\tnative-example\tamd64\t1\t' + digest + '\n'
    expected.write_text(native_record + ('host\texample\tdarwin-arm64\t1\t' + 'a' * 64 + '\n' if different else ''))
    events = []
    monkeypatch.setitem(namespace, 'dsc_fields', lambda path: ('test', '1', []))
    monkeypatch.setitem(namespace, 'sh', lambda *args, **kw: None)
    monkeypatch.setitem(namespace, 'in_root', lambda root, script, *args, **kwargs: [script, kwargs.get('staging')])
    monkeypatch.setitem(namespace, 'tree_state', lambda path: {})
    monkeypatch.setitem(namespace, 'orig_members', lambda *args: ({}, []))
    monkeypatch.setitem(namespace, 'collected_debs', lambda *args: ['test.deb'])
    def sudo(*args, **kwargs):
        if args[:2] == ('rm', '-rf') and Path(args[2]).name.startswith('.cross-deps-'):
            shutil.rmtree(args[2])
            events.append('captured')
        elif args[:2] == ('chown', '-R') and Path(args[3]).name == 'evidence':
            assert args[2] == '%d:%d' % (namespace['os'].getuid(), namespace['os'].getgid())
            assert events == ['resolved']
            events.append('owned')
    monkeypatch.setitem(namespace, 'sudo', sudo)
    original_rename = namespace['os'].rename
    def rename(source, destination):
        if Path(source).name == 'evidence':
            assert Path(destination) == out / 'cross-deps'
            assert events == (['resolved'] if unshare else ['resolved', 'owned'])
            events.append('exported')
        else:
            assert Path(source).name in ('base.lock', 'cross-deps.lock')
            assert Path(destination) == out / Path(source).name
        original_rename(source, destination)
    monkeypatch.setattr(namespace['os'], 'rename', rename)
    def external(argv, **kwargs):
        script = argv[0]
        if 'flock /work/.pie-cross-deps.lock' in script:
            assert 'apt-get update' in script and 'python3 /run/pie-cross/pie-cross-record' in script
            assert '--cross-deps-lock /run/pie-cross/reference/cross-deps.lock' in script
            assert '/usr/share/pietoolchain/pie-cross-deps' in script
            assert (Path(argv[1]) / 'reference/cross-deps.lock').read_bytes() == expected.read_bytes()
            if native_reference:
                assert (Path(argv[1]) / 'reference/archives' / (digest + '.deb')).read_bytes() == archive
            capture = Path(argv[1]) / 'evidence'
            capture.mkdir()
            (capture / 'receipt.json').write_text('{"captured": true}')
            (capture / 'base.lock').write_text('')
            (capture / 'cross-deps.lock').write_text(native_record)
            events.append('resolved')
        else:
            assert events == ['resolved'] + ([] if unshare else ['owned']) + ['exported', 'captured']
            assert json.loads((out / 'cross-deps/receipt.json').read_text()) == {'captured': True}
            events.append('compiled')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(namespace['subprocess'], 'run', external)
    if different:
        with pytest.raises(ValueError, match='example'):
            trace['run_trace'](str(root), str(dsc), str(out), 1, True, 'test', cross_deps_lock=str(expected))
    else:
        trace['run_trace'](str(root), str(dsc), str(out), 1, True, 'test', cross_deps_lock=str(expected))
        assert json.loads((out / 'meta.json').read_text())['cross_deps'] is True
    assert events == ['resolved'] + ([] if unshare else ['owned']) + ['exported', 'captured'] + ([] if different else ['compiled'])


@pytest.mark.parametrize('removal_fails', [False, True])
def test_build_parent_excludes_recorder_artifacts(tool, monkeypatch, tmp_path, removal_fails):
    import shutil
    from types import SimpleNamespace
    trace = tool('trace')
    namespace = trace['run_trace'].__globals__
    root = tmp_path / 'root'
    work = root / 'work/trace-test'
    work.mkdir(parents=True)
    dsc = tmp_path / 'test.dsc'
    dsc.write_text('source package')
    out = tmp_path / 'output'
    snapshots = []
    def sudo(*args, **kwargs):
        if args[:2] == ('rm', '-rf'):
            if Path(args[2]).name.startswith('.cross-deps-'):
                assert (out / 'cross-deps/receipt.json').read_text() == '{}'
                assert (out / 'cross-deps/archives/dependency.deb').read_bytes() == b'deb'
                if removal_fails:
                    raise subprocess.CalledProcessError(1, list(args))
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
            if destination.name == 'pie-cross-record':
                destination.write_text('recorder')
            elif destination.is_dir():
                shutil.copy(args[1], destination)
            else:
                shutil.copyfile(args[1], destination)
    def sh(*args, **kwargs):
        (work / 'src').mkdir()
    def external(argv, **kwargs):
        if 'flock /work/.pie-cross-deps.lock' in argv[0]:
            evidence = Path(argv[1]) / 'evidence'
            (evidence / 'archives').mkdir(parents=True)
            (evidence / 'status').write_text('resolver state')
            (evidence / 'receipt.json').write_text('{}')
            (evidence / 'base.lock').write_text('')
            (evidence / 'cross-deps.lock').write_text('')
            (evidence / 'archives/dependency.deb').write_bytes(b'deb')
        else:
            snapshots.append(sorted(p.name for p in work.iterdir()))
            (work / 'build.log').write_text('build output')
        return SimpleNamespace(returncode=0)
    monkeypatch.setitem(namespace, 'dsc_fields', lambda path: ('test', '1', []))
    monkeypatch.setitem(namespace, 'sudo', sudo)
    monkeypatch.setitem(namespace, 'sh', sh)
    monkeypatch.setitem(namespace, 'in_root', lambda root, script, *args, **kwargs: [script, kwargs.get('staging')])
    monkeypatch.setitem(namespace, 'tree_state', lambda path: {})
    monkeypatch.setitem(namespace, 'orig_members', lambda *args: ({}, []))
    monkeypatch.setitem(namespace, 'collected_debs', lambda *args: ['test.deb'])
    monkeypatch.setattr(namespace['subprocess'], 'run', external)
    if removal_fails:
        with pytest.raises(subprocess.CalledProcessError):
            trace['run_trace'](str(root), str(dsc), str(out), 1, True, 'test', cross_deps=True)
        assert snapshots == []
    else:
        trace['run_trace'](str(root), str(dsc), str(out), 1, True, 'test', cross_deps=True)
        assert snapshots == [['cross-root', 'src', 'test.dsc']]
    assert (out / 'cross-deps/status').read_text() == 'resolver state'
