# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from conftest import TOOLS
from pielib import root


@pytest.fixture
def recipe_file(tmp_path):
    directory = tmp_path / 'recipe'
    shutil.copytree(TOOLS / 'root-recipe', directory)
    path = directory / 'recipe.json'
    data = json.loads(path.read_text())
    data['packages'] = [next(pin for pin in data['packages'] if pin['package'] == 'dpkg-dev')]
    path.write_text(json.dumps(data))
    return path


def populate(recipe, path):
    status = path / 'var/lib/dpkg/status'
    status.parent.mkdir(parents=True)
    path.chmod(0o755)
    status.write_text('\n\n'.join('Package: ' + p['package'] + '\nArchitecture: ' + p['architecture']
                                  + '\nVersion: ' + p['version'] + '\nStatus: install ok installed\n'
                                  for p in recipe.data['packages']))
    (status.parent / 'arch').write_text('amd64\ndarwin-arm64\n')
    for name, data in zip(('etc/apt/sources.list', 'etc/apt/sources.list.d/ports.sources'), root.final_sources(recipe)):
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(data)
    for name, data in recipe.keys.items():
        file = path / 'usr/share/keyrings' / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    record = path / 'usr/share/pie/root-recipe.json'
    record.parent.mkdir(parents=True)
    record.write_bytes(recipe.canonical)
    program = path / 'usr/bin/dpkg-buildpackage'
    program.parent.mkdir(parents=True)
    program.write_text('fixture')
    program.chmod(0o755)


def fake_builder(monkeypatch, recipe, cache, temporary, failure=None):
    calls = []
    monkeypatch.setattr(root.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(root, 'require_temporary', lambda path: temporary)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert not (cache / recipe.digest).exists()
        if command[0] == 'mmdebstrap':
            if failure == 'bootstrap':
                raise subprocess.CalledProcessError(1, command)
            stage = kwargs['cwd']
            assert (stage / 'keys').stat().st_mode & 0o777 == 0o755
            assert (stage / 'mm-tmp').stat().st_mode & 0o7777 == 0o1777
            assert 'APT_CONFIG' not in kwargs['env']
            (stage / 'root.tar.xz').write_bytes(b'fixture')
        else:
            assert command[:6] == ['unshare', '--map-auto', '--map-root-user', 'tar', '--numeric-owner', '-xpf']
            target = Path(command[-1])
            populate(recipe, target)
            if failure == 'validation':
                (target / 'var/lib/dpkg/arch').write_text('amd64\n')
    monkeypatch.setattr(root, 'run', run)
    monkeypatch.setattr(root, 'remove_stage', lambda path: shutil.rmtree(path))
    return calls


def test_canonical_key_covers_recipe_keys_and_builder(recipe_file, monkeypatch):
    first = root.Recipe(recipe_file).digest
    data = json.loads(recipe_file.read_text())
    recipe_file.write_text(json.dumps(data, sort_keys=True, indent=4))
    assert root.Recipe(recipe_file).digest == first
    data['packages'][0]['version'] = '99'
    recipe_file.write_text(json.dumps(data))
    assert root.Recipe(recipe_file).digest != first
    data['packages'][0]['version'] = '1'
    recipe_file.write_text(json.dumps(data))
    before = root.Recipe(recipe_file).digest
    key = recipe_file.parent / 'keys/ports.gpg'
    key.write_bytes(key.read_bytes() + b'changed')
    assert root.Recipe(recipe_file).digest != before
    original = Path.read_bytes
    current = root.Recipe(recipe_file).digest
    monkeypatch.setattr(Path, 'read_bytes', lambda path: original(path) + (b'changed' if path == root.BUILDER_FILES[0] else b''))
    assert root.Recipe(recipe_file).digest != current


def test_key_is_independent_of_recipe_location(recipe_file, tmp_path):
    other = tmp_path / 'other'
    shutil.copytree(recipe_file.parent, other)
    assert root.Recipe(recipe_file).digest == root.Recipe(other / 'recipe.json').digest


def test_atomic_cache_publication_and_reuse(recipe_file, tmp_path, monkeypatch):
    recipe = root.Recipe(recipe_file)
    cache = tmp_path / 'cache'
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    calls = fake_builder(monkeypatch, recipe, cache, temporary)
    with monkeypatch.context() as environment:
        environment.setenv('APT_CONFIG', '/unused/sbuild.conf')
        result = root.build(recipe, cache, temporary)
    assert result == cache / recipe.digest / 'root'
    assert root.cached(recipe, cache) == result
    assert root.build(recipe, cache, temporary) == result
    assert len(calls) == 2
    assert not list(temporary.iterdir())
    assert not list(cache.glob('.' + recipe.digest + '-*'))
    assert '--mode=unshare' in calls[0][0]
    assert '--variant=essential' in calls[0][0]
    assert '--skip=output/mknod' in calls[0][0]
    assert any('dpkg --add-architecture darwin-arm64' in item for item in calls[0][0])


@pytest.mark.parametrize('failure', ['bootstrap', 'validation'])
def test_failed_build_is_not_published(recipe_file, tmp_path, monkeypatch, failure):
    recipe = root.Recipe(recipe_file)
    cache, temporary = tmp_path / 'cache', tmp_path / 'temporary'
    temporary.mkdir()
    fake_builder(monkeypatch, recipe, cache, temporary, failure)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        root.build(recipe, cache, temporary)
    assert not (cache / recipe.digest).exists()
    assert not list(temporary.iterdir())
    assert list(cache.iterdir()) == [cache / (recipe.digest + '.lock')]


@pytest.mark.parametrize('kind', ['incomplete', 'foreign', 'symlink', 'wrong-recipe'])
def test_foreign_or_incomplete_cache_is_refused(recipe_file, tmp_path, monkeypatch, kind):
    recipe = root.Recipe(recipe_file)
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    entry = cache / recipe.digest
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    if kind == 'symlink':
        entry.symlink_to(foreign, target_is_directory=True)
    else:
        entry.mkdir(mode=0o700)
        populate(recipe, entry / 'root')
        if kind != 'incomplete':
            (entry / 'recipe.json').write_bytes(recipe.canonical if kind == 'foreign' else b'wrong')
            (entry / 'complete.json').write_text(json.dumps({'schema': 1, 'recipe_sha256': '0' * 64 if kind == 'foreign' else recipe.digest, 'complete': True}))
    before = set(tmp_path.rglob('*'))
    monkeypatch.setattr(root, 'run', lambda *a, **kw: pytest.fail('a foreign root must not be touched'))
    with pytest.raises(ValueError, match='incomplete or foreign'):
        root.cached(recipe, cache)
    with pytest.raises(ValueError, match='incomplete or foreign'):
        root.build(recipe, cache, tmp_path)
    assert set(tmp_path.rglob('*')) - before == {cache / (recipe.digest + '.lock')}


def test_private_staging_ancestor_reports_setting(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='PIE_ROOT_TMPDIR'):
        root.require_temporary(private)
    assert root.require_temporary(Path('/var/tmp')) == Path('/var/tmp')


def test_missing_tool_is_reported_without_building(recipe_file, tmp_path, monkeypatch):
    monkeypatch.setattr(root.shutil, 'which', lambda name: None)
    with pytest.raises(RuntimeError, match='missing required tool: mmdebstrap'):
        root.build(root.Recipe(recipe_file), tmp_path / 'cache', tmp_path)


def test_cache_setting_and_cli_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    assert root.default_cache() == tmp_path / 'pie/roots'
    direct = subprocess.run([str(TOOLS / 'pie-root'), 'hash'], capture_output=True, text=True)
    dispatch = subprocess.run([str(TOOLS / 'pie'), 'root', 'hash'], capture_output=True, text=True)
    assert direct.returncode == dispatch.returncode == 0
    assert direct.stdout == dispatch.stdout == root.Recipe(root.DEFAULT_RECIPE).digest + '\n'


def test_fixed_proof_home_keeps_account_cache_default(tmp_path, monkeypatch):
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    monkeypatch.setenv('HOME', '/nonexistent')
    monkeypatch.setattr(root.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    assert root.default_cache() == tmp_path / '.cache/pie/roots'
    monkeypatch.setenv('XDG_CACHE_HOME', '')
    assert root.default_cache() == tmp_path / '.cache/pie/roots'


def test_recipe_contains_required_tooling_and_archives():
    recipe = root.Recipe(root.DEFAULT_RECIPE)
    pins = {p['package']: p['version'] for p in recipe.data['packages']}
    for name in ('mount', 'util-linux', 'procps', 'strace', 'python3-debian', 'pietoolchain-host', 'pietoolchain-sdk'):
        assert name in pins
    assert pins['mount'] == pins['util-linux']
    assert 'pietoolchain' not in pins
    assert pins['pietoolchain-host'] == '0.3.2'
    assert pins['pietoolchain-sdk'] == '0.3.2'
    command = root.bootstrap_command(recipe, Path('/var/tmp/root-stage'))
    for line in command:
        if line.startswith('deb '):
            assert 'signed-by=/var/tmp/root-stage/keys/' in line
    native, target = root.final_sources(recipe)
    assert native.count('arch=amd64') == 3
    assert 'Architectures: darwin-arm64' in target
    assert '/var/tmp/' not in native + target


def test_working_copy_preserves_cache_and_uses_independent_files(recipe_file, tmp_path, monkeypatch):
    recipe = root.Recipe(recipe_file)
    cache, temporary = tmp_path / 'cache', tmp_path / 'temporary'
    temporary.mkdir()
    fake_builder(monkeypatch, recipe, cache, temporary)
    source = root.build(recipe, cache, temporary)
    link = source / 'status-link'
    link.symlink_to('var/lib/dpkg/status')
    original = (source / 'var/lib/dpkg/status').read_bytes()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[:6] == ['unshare', '--map-auto', '--map-root-user', 'cp', '--archive', '--reflink=auto']
        subprocess.run(command[3:], check=True)
    monkeypatch.setattr(root, 'run', run)
    destination = cache / ('.proof-' + recipe.digest + '-test')
    destination.mkdir(mode=0o700)
    working = root.copy_working(recipe, cache, destination)
    assert not os.path.samefile(source / 'var/lib/dpkg/status', working / 'var/lib/dpkg/status')
    assert (working / 'status-link').is_symlink()
    (working / 'var/lib/dpkg/status').write_text('installed by a proof\n')
    (working / 'work').mkdir()
    assert (source / 'var/lib/dpkg/status').read_bytes() == original
    assert not (source / 'work').exists()
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['foreign-name', 'populated', 'symlink'])
def test_working_copy_refuses_unknown_destination(recipe_file, tmp_path, monkeypatch, kind):
    recipe = root.Recipe(recipe_file)
    cache, temporary = tmp_path / 'cache', tmp_path / 'temporary'
    temporary.mkdir()
    fake_builder(monkeypatch, recipe, cache, temporary)
    root.build(recipe, cache, temporary)
    destination = cache / ('.proof-' + recipe.digest + '-test' if kind != 'foreign-name' else 'unknown')
    if kind == 'symlink':
        destination.symlink_to(temporary, target_is_directory=True)
    else:
        destination.mkdir(mode=0o700)
        if kind == 'populated':
            (destination / 'retained').write_text('keep')
    monkeypatch.setattr(root, 'run', lambda *a, **kw: pytest.fail('unknown destinations must not be changed'))
    with pytest.raises(ValueError):
        root.copy_working(recipe, cache, destination)
