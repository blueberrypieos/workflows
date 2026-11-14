# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import shlex
import time

import pytest

from conftest import TOOLS


@pytest.mark.parametrize('interrupt', [False, True])
def test_trace_children_stop(tmp_path, interrupt):
    stub = tmp_path / 'pie-trace'
    stub.write_text('''#!/bin/bash
if [[ " $* " == *" --slot F "* ]]; then
    sleep 30 &
    echo $! > "$CHILD"
    wait
else
    while [ ! -s "$CHILD" ]; do sleep 0.01; done
    if [ "$INTERRUPT" = yes ]; then sleep 30; else exit 1; fi
fi
''')
    stub.chmod(0o755)
    source = (TOOLS / 'pie-prove').read_text()
    block = source[source.index('fpid=\n'):source.index('if [ -n "$resume" ]; then\n\t# The full')]
    setup = '''set -euo pipefail
name=test src=test ver=1 jobs=1
root=/unused
checkonly= resume=
leave=() trace_args=()
cleanup_root() { :; }
hd=$HD tools=$TOOLS out=$OUT
'''
    proc = subprocess.Popen(['bash', '-c', setup + block], start_new_session=True,
                            env=dict(os.environ, HD=str(tmp_path / 'hd'), TOOLS=str(tmp_path),
                                     OUT=str(tmp_path), CHILD=str(tmp_path / 'child'),
                                     INTERRUPT='yes' if interrupt else 'no'))
    child = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (tmp_path / 'child').exists():
                child = int((tmp_path / 'child').read_text())
                break
            time.sleep(0.01)
        assert child is not None
        if interrupt:
            proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) != 0
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stat = Path('/proc/%d/stat' % child)
            if not stat.exists() or stat.read_text().split()[2] == 'Z':
                break
            time.sleep(0.01)
        else:
            pytest.fail('the trace child is still running')
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('mode', ['CHECK_ONLY', 'RESUME'])
@pytest.mark.parametrize('recorded', [(False, False), (False, True), (True, False), (True, True)])
def test_reused_traces_require_private_mounts(tmp_path, mode, recorded):
    import json
    for side, current in zip(('F', 'R'), recorded):
        out = tmp_path / ('test-t' + side)
        out.mkdir()
        metadata = {'expected_debs': ['test.deb']}
        if current:
            metadata['private_tmpfs'] = {'/tmp': '1777', '/var/tmp': '1777', '/run': '755'}
        (out / 'meta.json').write_text(json.dumps(metadata))
    (tmp_path / 'test-commit.txt').write_text('test-head\n')
    source = (TOOLS / 'pie-prove').read_text()
    block = source[source.index('resume= checkonly='):source.index('# The full and reduced builds use separate slots')]
    setup = 'set -euo pipefail\nname=test head=test-head\nout=$OUT tools=$TOOLS\n'
    result = subprocess.run(['bash', '-c', setup + block + 'printf "REUSE_ACCEPTED\\n"'],
                            env=dict(os.environ, OUT=str(tmp_path), TOOLS=str(TOOLS),
                                     PIE_PROVE_CHECK_ONLY='1' if mode == 'CHECK_ONLY' else '',
                                     PIE_PROVE_RESUME='1' if mode == 'RESUME' else '',
                                     PIE_PROVE_SOURCES=''), capture_output=True, text=True, timeout=5)
    accepted = all(recorded) if mode == 'CHECK_ONLY' else recorded[0]
    if accepted:
        assert result.returncode == 0, result.stderr
        assert result.stdout == 'REUSE_ACCEPTED\n'
    else:
        assert result.returncode != 0
        assert 'REUSE_ACCEPTED' not in result.stdout
        assert 'rebuild' in result.stderr
        side = 'F' if not recorded[0] else 'R'
        assert str(tmp_path / ('test-t' + side)) in result.stderr
        for path in ('/tmp', '/var/tmp', '/run'):
            assert path in result.stderr


@pytest.mark.parametrize('content', [None, 'not JSON', '[]', '{}',
                                     '{"private_tmpfs": true}', '{"private_tmpfs": null}',
                                     '{"private_tmpfs": {"/tmp": "1777"}}',
                                     '{"private_tmpfs": {"/tmp": "1777", "/var/tmp": "1777", "/run": "1777"}}'])
def test_incomplete_private_mount_record(tmp_path, content):
    from pielib.proof import require_private_tmpfs
    if content is not None:
        (tmp_path / 'meta.json').write_text(content)
    with pytest.raises(SystemExit, match='rebuild it without PIE_PROVE_CHECK_ONLY or PIE_PROVE_RESUME'):
        require_private_tmpfs([str(tmp_path)])


@pytest.mark.parametrize('dispatcher', [False, True])
@pytest.mark.parametrize('arguments', [[], ['repo'], ['', 'test'], ['repo', '']])
def test_prove_requires_repo_and_name(tmp_path, dispatcher, arguments):
    repo = tmp_path / 'repo'
    repo.mkdir()
    out = tmp_path / 'out'
    out.mkdir()
    report = out / 'test-prove.txt'
    report.write_text('previous report\n')
    before = set(tmp_path.rglob('*'))
    command = [str(TOOLS / 'pie'), 'prove'] if dispatcher else [str(TOOLS / 'pie-prove')]
    result = subprocess.run(command + arguments, cwd=tmp_path,
                            env=dict(os.environ, PIE_OUT=str(out)), capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert 'pie-prove: REPO and NAME are required' in result.stderr
    assert 'usage: pie-prove REPO NAME [CHROOT] [JOBS]' in result.stderr
    assert not result.stdout
    assert report.read_text() == 'previous report\n'
    assert set(tmp_path.rglob('*')) == before


@pytest.mark.parametrize('dispatcher', [False, True])
def test_prove_uses_explicit_chroot(tmp_path, dispatcher):
    root = tmp_path / 'root'
    root.mkdir()
    command = [str(TOOLS / 'pie'), 'prove'] if dispatcher else [str(TOOLS / 'pie-prove')]
    result = subprocess.run(command + [str(tmp_path), 'test', str(root)], cwd=tmp_path,
                            env=dict(os.environ, PIE_OUT=str(tmp_path / 'out')),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert result.stderr == 'pie-prove: CHROOT has no dpkg-buildpackage: %s\n' % root


@pytest.mark.parametrize('arguments,default,jobs', [([], True, 8), ([''], True, 8), (['-'], True, 8),
                                                  (['-', '4'], True, 4), (['ROOT'], False, 8),
                                                  (['ROOT', '3'], False, 3)])
def test_root_arguments_and_report_line(tmp_path, arguments, default, jobs):
    tools = tmp_path / 'tools'
    tools.mkdir()
    root = tmp_path / 'cache' / ('a' * 64) / 'root'
    (root / 'usr/bin').mkdir(parents=True)
    program = root / 'usr/bin/dpkg-buildpackage'
    program.write_text('fixture')
    program.chmod(0o755)
    calls = tmp_path / 'root-calls'
    helper = tools / 'pie-root'
    helper.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$1" >> ' + shlex.quote(str(calls))
                      + '\ncase "$1" in\nbuild) printf "%s\\n" ' + shlex.quote(str(root))
                      + ';;\nhash) printf "%s\\n" ' + 'a' * 64
                      + ';;\ncopy) mkdir "$3/root"; cp -a ' + shlex.quote(str(root) + '/.') + ' "$3/root";;\nesac\n')
    helper.chmod(0o755)
    unshare = tools / 'unshare'
    unshare.write_text('#!/bin/sh\nset -eu\ntest "$1" = --map-auto\ntest "$2" = --map-root-user\nshift 2\ntest "$1" = rm\nexec "$@"\n')
    unshare.chmod(0o755)
    source = (TOOLS / 'pie-prove').read_text()
    prefix = source[:source.index('export GIT_NO_REPLACE_OBJECTS=1')]
    start = source.index('{\n\techo "== $src')
    header = source[start:source.index("\tsed -n '2,3p;5p;8,9p'", start)]
    script = tools / 'pie-prove'
    script.write_text(prefix + 'src=test ver=1\nmkdir -p "$out"\n' + header
                      + '} | tee "$report"\nprintf "jobs=%s unshare=%s\\n" "$jobs" "${PIE_UNSHARE:-}"\n')
    script.chmod(0o755)
    env = dict(os.environ, PIE_OUT=str(tmp_path / 'out'), PATH=str(tools) + os.pathsep + os.environ['PATH'])
    env.pop('PIE_UNSHARE', None)
    args = [str(root) if item == 'ROOT' else item for item in arguments]
    result = subprocess.run([str(script), str(tmp_path), 'test', *args], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = (tmp_path / 'out/test-prove.txt').read_text()
    assert ('root: recipe sha256:' + 'a' * 64 if default else 'root: explicit ' + str(root)) in report.splitlines()
    assert ('jobs=%d unshare=%s' % (jobs, '1' if default else '')) in result.stdout
    assert (calls.read_text() if calls.exists() else '') == ('build\nhash\ncopy\n' if default else '')
    assert program.read_text() == 'fixture'
    assert not list(root.parent.parent.glob('.proof-*'))


@pytest.mark.parametrize('arguments', [['7'], ['-', '0'], ['-', 'bad'], ['ROOT', '1', 'extra']])
def test_ambiguous_or_invalid_jobs_do_not_start_a_root(tmp_path, arguments):
    report = tmp_path / 'test-prove.txt'
    report.write_text('previous report\n')
    result = subprocess.run([str(TOOLS / 'pie-prove'), str(tmp_path), 'test', *arguments],
                            env=dict(os.environ, PIE_OUT=str(tmp_path)), capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert report.read_text() == 'previous report\n'


def isolated_proof_prefix(tmp_path, copy_mode='normal', phase='early'):
    from pielib.root import Recipe, DEFAULT_RECIPE, canonical
    recipe = Recipe(DEFAULT_RECIPE)
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    entry = cache / recipe.digest
    entry.mkdir(mode=0o700)
    cached = entry / 'root'
    cached.mkdir(mode=0o755)
    for name, content in (('usr/bin/dpkg-buildpackage', 'fixture'), ('var/lib/dpkg/status', 'pristine status\n')):
        path = cached / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (cached / 'usr/bin/dpkg-buildpackage').chmod(0o755)
    (entry / 'recipe.json').write_bytes(recipe.canonical)
    (entry / 'complete.json').write_bytes(canonical({'schema': 1, 'recipe_sha256': recipe.digest, 'complete': True}))
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'pie-root').symlink_to(TOOLS / 'pie-root')
    out = tmp_path / 'out'
    out.mkdir()
    unshare = tools / 'unshare'
    copy = 'exec "$@"'
    if copy_mode != 'normal':
        copy = 'mkdir -p "${@: -1}"; printf partial > "${@: -1}/partial"; '
        copy += 'exit 17' if copy_mode == 'fail' else ('touch ' + shlex.quote(str(out / 'copy-started')) + '; exec sleep 30')
    unshare.write_text('#!/bin/bash\nset -eu\ntest "$1" = --map-auto\ntest "$2" = --map-root-user\nshift 2\n'
                       'case "$1" in\ncp) ' + copy + ';;\nrm) exec "$@";;\n*) exit 99;;\nesac\n')
    unshare.chmod(0o755)
    source = (TOOLS / 'pie-prove').read_text()
    prefix = source[:source.index('export GIT_NO_REPLACE_OBJECTS=1')]
    if phase != 'early':
        prefix += 'hd=$(mktemp -d "$out/headers.XXXXXX")\n'
        if phase == 'headers':
            prefix += next(line for line in source.splitlines() if line.startswith('trap ') and '"$hd"' in line and 'stop_traces' not in line) + '\n'
        else:
            prefix += source[source.index('fpid=\n'):source.index('if [ -n "$checkonly" ]; then\n\trm -f "$out/$name-tR.txt"')]
    script = tools / 'pie-prove'
    env = dict(os.environ, PIE_ROOT_CACHE=str(cache), PIE_OUT=str(out), PATH=str(tools) + os.pathsep + os.environ['PATH'])
    for name in ('PIE_PROVE_CHECK_ONLY', 'PIE_PROVE_RESUME', 'PIE_ROOT_RECIPE'):
        env.pop(name, None)
    return script, prefix, cached, out, env


def cached_state(path):
    return {str(file.relative_to(path)): (file.lstat().st_mode,
            hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None)
            for file in [path, *path.rglob('*')]}


@pytest.mark.parametrize('phase', ['early', 'headers', 'traces'])
@pytest.mark.parametrize('result', ['success', 'failure', 'TERM', 'INT', 'HUP'])
def test_default_proof_uses_copy_and_cleans_every_exit(tmp_path, phase, result):
    script, prefix, cached, out, env = isolated_proof_prefix(tmp_path, phase=phase)
    before = cached_state(cached)
    body = ('printf "%s\\n" "$root" > "$out/used-root"\n'
            'printf changed >> "$root/var/lib/dpkg/status"\n'
            'mkdir -p "$root/var/cache/apt" "$root/work/trace-test@F"\n'
            'printf downloaded > "$root/var/cache/apt/proof"\ntouch "$out/ready"\n')
    body += 'exit ' + ('0' if result == 'success' else '7') + '\n' if result in ('success', 'failure') else 'while :; do sleep 0.01; done\n'
    script.write_text(prefix + body)
    script.chmod(0o755)
    proc = subprocess.Popen([str(script), str(tmp_path), 'test'], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not (out / 'ready').exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if result not in ('success', 'failure'):
            assert (out / 'ready').exists(), proc.communicate(timeout=1)
            proc.send_signal(getattr(signal, 'SIG' + result))
        stdout, stderr = proc.communicate(timeout=5)
        expected = 0 if result == 'success' else 7 if result == 'failure' else 128 + getattr(signal, 'SIG' + result)
        assert proc.returncode == expected, stdout + stderr
        working = Path((out / 'used-root').read_text().strip())
        assert working != cached
        assert not working.parent.exists()
        assert not list(out.glob('headers.*'))
        assert cached_state(cached) == before
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


@pytest.mark.parametrize('mode', ['fail', 'signal'])
def test_failed_or_interrupted_copy_is_removed(tmp_path, mode):
    script, prefix, cached, out, env = isolated_proof_prefix(tmp_path, copy_mode='fail' if mode == 'fail' else 'wait')
    before = cached_state(cached)
    script.write_text(prefix + 'exit 0\n')
    script.chmod(0o755)
    proc = subprocess.Popen([str(script), str(tmp_path), 'test'], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        if mode == 'signal':
            deadline = time.monotonic() + 5
            while not (out / 'copy-started').exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (out / 'copy-started').exists(), proc.communicate(timeout=1)
            proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == (143 if mode == 'signal' else 1), stdout + stderr
        assert not list(cached.parent.parent.glob('.proof-*'))
        assert cached_state(cached) == before
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


@pytest.mark.parametrize('variable', ['PIE_PROVE_RESUME', 'PIE_PROVE_CHECK_ONLY'])
def test_default_root_refuses_reuse_of_discarded_build_state(tmp_path, variable):
    result = subprocess.run([str(TOOLS / 'pie-prove'), str(tmp_path), 'test'],
                            env=dict(os.environ, **{variable: '1', 'PIE_OUT': str(tmp_path / 'out')}),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'working copy is removed' in result.stderr


@pytest.mark.parametrize('mismatch', [False, True])
def test_locked_proof_waits_for_full_and_checks_reduced(tmp_path, mismatch):
    import hashlib
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'pielib').symlink_to(TOOLS / 'pielib', target_is_directory=True)
    script = tools / 'pie-trace'
    script.write_text('''#!/bin/bash
set -eu
if [[ " $* " == *" --slot F "* ]]; then
    sleep 0.05
    mkdir "$OUT/test-tF"
    printf 'host\\texample\\tdarwin-arm64\\t1\\t%s\\n' "$SHA" > "$OUT/test-tF/cross-deps.lock"
    touch "$OUT/full-finished"
else
    test -f "$OUT/full-finished"
    [[ " $* " == *" --cross-deps-lock $OUT/test-tF/cross-deps.lock "* ]]
    mkdir "$OUT/test-tR"
    cp "$OUT/test-tF/cross-deps.lock" "$OUT/test-tR/cross-deps.lock"
    if [ "$MISMATCH" = yes ]; then sed -i s/example/different/ "$OUT/test-tR/cross-deps.lock"; fi
fi
''')
    script.chmod(0o755)
    source = (TOOLS / 'pie-prove').read_text()
    block = source[source.index('fpid=\n'):source.index('if [ -n "$resume" ]; then\n\t# The full')]
    setup = '''set -euo pipefail
cleanup_root() { :; }
name=test src=test ver=1 jobs=1
root=/unused
checkonly= resume=
leave=() trace_args=(--cross-deps)
hd=$OUT/hd tools=$TOOLS out=$OUT
'''
    result = subprocess.run(['bash', '-c', setup + block], capture_output=True, text=True,
                            env=dict(os.environ, OUT=str(tmp_path), TOOLS=str(tools),
                                     SHA=hashlib.sha256(b'dep').hexdigest(), MISMATCH='yes' if mismatch else 'no'))
    assert (result.returncode != 0) == mismatch, result.stderr
    if mismatch:
        assert 'example' in result.stderr and 'different' in result.stderr


@pytest.mark.parametrize('mode', ['check', 'resume'])
@pytest.mark.parametrize('evidence', ['lock', 'metadata', 'log', 'directory'])
@pytest.mark.parametrize('mismatch', [False, True])
def test_retained_locks_are_enforced_without_repeating_cross_deps(tmp_path, mode, evidence, mismatch):
    import json
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'pielib').symlink_to(TOOLS / 'pielib', target_is_directory=True)
    program = tools / 'pie-trace'
    program.write_text('''#!/bin/bash
set -eu
if [[ " $* " != *" --analyse "* ]]; then
    [[ " $* " == *" --cross-deps "* ]]
    [[ " $* " == *" --cross-deps-lock $OUT/test-tF/cross-deps.lock "* ]]
    cp "$OUT/test-tF/cross-deps.lock" "$OUT/test-tR/cross-deps.lock"
    if [ "$MISMATCH" = yes ]; then sed -i 's/\\t1\\t/\\t2\\t/' "$OUT/test-tR/cross-deps.lock"; fi
fi
''')
    program.chmod(0o755)
    for side in ('F', 'R'):
        trace = tmp_path / ('test-t' + side)
        trace.mkdir()
        version = '2' if side == 'R' and mismatch else '1'
        (trace / 'cross-deps.lock').write_text('host\texample\tdarwin-arm64\t' + version + '\t' + 'a' * 64 + '\n')
        if evidence == 'metadata':
            (trace / 'meta.json').write_text(json.dumps({'cross_deps': True}))
        elif evidence == 'log':
            (trace / 'cross-deps.log').write_text('dependency preparation ran\n')
        elif evidence == 'directory':
            (trace / 'cross-deps').mkdir()
    source = (TOOLS / 'pie-prove').read_text()
    block = source[source.index('fpid=\n'):source.index('if [ -n "$resume" ]; then\n\t# The full')]
    setup = '''set -euo pipefail
cleanup_root() { :; }
name=test src=test ver=1 jobs=1 root=/unused
resume=1 checkonly=$CHECK
leave=() trace_args=()
hd=$OUT/hd tools=$TOOLS out=$OUT
'''
    result = subprocess.run(['bash', '-c', setup + block + '\necho ACCEPTED'], capture_output=True, text=True,
                            env=dict(os.environ, OUT=str(tmp_path), TOOLS=str(tools), CHECK='1' if mode == 'check' else '',
                                     MISMATCH='yes' if mismatch else 'no'))
    assert (result.returncode != 0) == mismatch, result.stderr
    if mismatch:
        assert 'cross dependency lock differs' in result.stderr
        assert 'ACCEPTED' not in result.stdout
    else:
        assert result.stdout == 'ACCEPTED\n'


@pytest.mark.parametrize('mode,missing', [('check', 'F'), ('check', 'R'), ('resume', 'F'), ('check', 'both'), ('resume', 'both')])
@pytest.mark.parametrize('evidence', ['metadata', 'log', 'directory'])
def test_retained_dependency_evidence_rejects_missing_locks(tmp_path, mode, missing, evidence):
    import json
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'pielib').symlink_to(TOOLS / 'pielib', target_is_directory=True)
    program = tools / 'pie-trace'
    program.write_text('#!/bin/sh\nexit 0\n')
    program.chmod(0o755)
    for side in ('F', 'R'):
        trace = tmp_path / ('test-t' + side)
        trace.mkdir()
        if side != missing and missing != 'both':
            (trace / 'cross-deps.lock').write_text('')
        if evidence == 'metadata':
            (trace / 'meta.json').write_text(json.dumps({'cross_deps': True}))
        elif evidence == 'log':
            (trace / 'cross-deps.log').write_text('dependency preparation ran\n')
        else:
            (trace / 'cross-deps').mkdir()
    source = (TOOLS / 'pie-prove').read_text()
    block = source[source.index('fpid=\n'):source.index('if [ -n "$resume" ]; then\n\t# The full')]
    setup = '''set -euo pipefail
cleanup_root() { :; }
name=test src=test ver=1 jobs=1 root=/unused
resume=1 checkonly=$CHECK
leave=() trace_args=()
hd=$OUT/hd tools=$TOOLS out=$OUT
'''
    result = subprocess.run(['bash', '-c', setup + block], capture_output=True, text=True,
                            env=dict(os.environ, OUT=str(tmp_path), TOOLS=str(tools), CHECK='1' if mode == 'check' else ''))
    assert result.returncode != 0
    assert 'cross-deps.lock' in result.stderr
