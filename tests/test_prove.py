# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import os
from pathlib import Path
import signal
import subprocess
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
    block = source[source.index('resume= checkonly='):source.index('# The full and the reduced build run at once')]
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


@pytest.mark.parametrize('chroot,expected', [([], 2), ([''], 2), (['CHROOT'], 0)])
def test_explicit_chroot_argument_is_required(tmp_path, chroot, expected):
    source = (TOOLS / 'pie-prove').read_text()
    start = source.index('if [ $# -lt')
    block = source[start:source.index('\nout=', start)]
    script = tmp_path / 'arguments.sh'
    script.write_text('set -eu\n' + block + '\nprintf "%s\\n" "$root"\n')
    result = subprocess.run(['bash', str(script), 'REPO', 'NAME', *chroot], capture_output=True, text=True)
    assert result.returncode == expected
    assert result.stdout == ('CHROOT\n' if expected == 0 else '')
