# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: Recipe(FILE), build(recipe, cache, temporary), cached(recipe, cache): manage recipe-built roots."""
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from debian.deb822 import Deb822

TOOLS = Path(__file__).resolve().parent.parent
DEFAULT_RECIPE = TOOLS / 'root-recipe/recipe.json'
BUILDER_FILES = (Path(__file__), TOOLS / 'pielib/commands/root.py', TOOLS / 'pie-root')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode() + b'\n'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Recipe:
    def __init__(self, path):
        path = Path(path)
        self.data = json.loads(path.read_text())
        data = self.data
        if (data['schema'] != 1 or data['variant'] != 'essential'
                or data['architecture'] != 'amd64' or data['foreign_architectures'] != ['darwin-arm64']
                or not re.fullmatch(r'\d{8}T\d{6}Z', data['ubuntu_snapshot'])):
            raise ValueError('unsupported root recipe')
        self.keys = {}
        for name, relative in data['keyrings'].items():
            location = Path(relative)
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', name) or location.is_absolute()
                    or '..' in location.parts or not (path.parent / location).resolve().is_relative_to(path.parent.resolve())):
                raise ValueError('keyring must be a relative file inside the recipe directory')
            self.keys[name] = (path.parent / location).read_bytes()
        packages = data['packages']
        if not packages or len({p['package'] for p in packages}) != len(packages):
            raise ValueError('root recipe needs unique pinned packages')
        for package in packages:
            if (not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', package['package'])
                    or package['architecture'] not in ('amd64', 'all')
                    or not re.fullmatch(r'[0-9A-Za-z.+:~_-]+', package['version'])):
                raise ValueError('invalid package pin')
        snapshot = 'https://snapshot.ubuntu.com/ubuntu/' + data['ubuntu_snapshot']
        if not any(source['uri'] == snapshot for source in data['sources']):
            raise ValueError('root recipe must use its dated Ubuntu snapshot')
        for source in data['sources']:
            if (source['keyring'] not in self.keys or not source['architectures']
                    or not set(source['architectures']) <= {'amd64', 'darwin-arm64'}
                    or any('\n' in str(v) or '\r' in str(v) for v in source.values())):
                raise ValueError('invalid signed apt source')
        self.record = {'recipe': data, 'keyrings': {name: sha(value) for name, value in self.keys.items()},
                       'builder': {str(file.relative_to(TOOLS)): sha(file.read_bytes()) for file in BUILDER_FILES}}
        self.canonical = canonical(self.record)
        self.digest = sha(self.canonical)


def default_cache():
    home = Path.home()
    if str(home) == '/nonexistent':
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return Path(os.environ.get('XDG_CACHE_HOME') or str(home / '.cache')) / 'pie/roots'


def owned_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('cache directory must be owned by the current user and not writable by others: ' + str(path))


def cached(recipe, cache):
    entry = Path(cache).expanduser().absolute() / recipe.digest
    try:
        owned_directory(entry)
        root = entry / 'root'
        owned_directory(root)
        marker = entry / 'complete.json'
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('invalid completion record')
        record = json.loads(marker.read_text())
        if (record != {'schema': 1, 'recipe_sha256': recipe.digest, 'complete': True}
                or (entry / 'recipe.json').is_symlink() or (entry / 'recipe.json').read_bytes() != recipe.canonical
                or not os.access(root / 'usr/bin/dpkg-buildpackage', os.X_OK)):
            raise ValueError('invalid completed root')
    except (OSError, ValueError) as error:
        raise ValueError('missing, incomplete or foreign root cache entry; run pie root build for a missing entry: ' + str(entry)) from error
    return root


def sources(recipe, host_keys=None):
    result = []
    for source in recipe.data['sources']:
        key = str(host_keys / source['keyring']) if host_keys else '/usr/share/keyrings/' + source['keyring']
        if any(character.isspace() for character in key):
            key = json.dumps(key)
        result.append('deb [arch=' + ','.join(source['architectures']) + ' signed-by=' + key + '] '
                      + source['uri'] + ' ' + source['suite'] + ' ' + ' '.join(source['components']))
    return result


def final_sources(recipe):
    native, target = [], []
    for source, line in zip(recipe.data['sources'], sources(recipe)):
        if source.get('bootstrap_only'):
            continue
        if source['architectures'] == ['darwin-arm64']:
            target.append('Types: deb\nURIs: ' + source['uri'] + '\nSuites: ' + source['suite']
                          + '\nComponents: ' + ' '.join(source['components'])
                          + '\nArchitectures: darwin-arm64\nSigned-By: /usr/share/keyrings/' + source['keyring'] + '\n')
        else:
            native.append(line)
    return '\n'.join(native) + '\n', '\n'.join(target)


def bootstrap_command(recipe, stage):
    keydir = stage / 'keys'
    setup = 'mkdir -p "$1/usr/share/keyrings"; ' + '; '.join(
        'cp ' + shlex.quote(str(keydir / name)) + ' "$1/usr/share/keyrings/' + name + '"' for name in recipe.keys)
    customize = ('mkdir -p "$1/etc/apt/sources.list.d" "$1/usr/share/pie"; cp '
                 + shlex.quote(str(stage / 'sources.list')) + ' "$1/etc/apt/sources.list"; cp '
                 + shlex.quote(str(stage / 'ports.sources')) + ' "$1/etc/apt/sources.list.d/ports.sources"; cp '
                 + shlex.quote(str(stage / 'recipe.json')) + ' "$1/usr/share/pie/root-recipe.json"; '
                 + 'chroot "$1" dpkg --add-architecture darwin-arm64')
    return ['mmdebstrap', '--verbose', '--mode=unshare', '--variant=essential', '--skip=output/mknod',
            '--architectures=amd64,darwin-arm64',
            '--include=' + ','.join(p['package'] + '=' + p['version'] for p in recipe.data['packages']),
            '--setup-hook=' + setup, '--customize-hook=' + customize,
            recipe.data['suite'], str(stage / 'root.tar.xz'), *sources(recipe, keydir)]


def validate_root(recipe, root):
    with (root / 'var/lib/dpkg/status').open() as stream:
        installed = {(p['Package'], p['Architecture']): p['Version'] for p in Deb822.iter_paragraphs(stream)
                     if p.get('Status') == 'install ok installed'}
    for pin in recipe.data['packages']:
        if installed.get((pin['package'], pin['architecture'])) != pin['version']:
            raise ValueError('root is missing exact pin: ' + pin['package'] + '=' + pin['version'])
    if (root / 'var/lib/dpkg/arch').read_text().splitlines() != ['amd64', 'darwin-arm64']:
        raise ValueError('root has incorrect dpkg architectures')
    native, target = final_sources(recipe)
    for filename, expected in (('etc/apt/sources.list', native), ('etc/apt/sources.list.d/ports.sources', target)):
        if (root / filename).read_text() != expected:
            raise ValueError('root apt sources differ: ' + filename)
    for name, data in recipe.keys.items():
        if (root / 'usr/share/keyrings' / name).read_bytes() != data:
            raise ValueError('root keyring differs: ' + name)
    if (root / 'usr/share/pie/root-recipe.json').read_bytes() != recipe.canonical:
        raise ValueError('root recipe record differs')
    if not os.access(root / 'usr/bin/dpkg-buildpackage', os.X_OK):
        raise ValueError('root has no dpkg-buildpackage')


def run(command, **kwargs):
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=sys.stderr, **kwargs)


def remove_stage(path):
    run(['unshare', '--map-auto', '--map-root-user', 'rm', '-rf', '--', str(path)])


def require_temporary(temporary):
    temporary = Path(temporary).expanduser().absolute()
    for path in (temporary, *temporary.parents):
        if not path.is_dir() or not path.stat().st_mode & stat.S_IXOTH:
            raise ValueError('PIE_ROOT_TMPDIR must have world-traversable ancestors: ' + str(temporary))
    return temporary


def copy_working(recipe, cache, destination):
    source = cached(recipe, cache)
    destination = Path(destination).absolute()
    if (destination.parent != source.parent.parent
            or not destination.name.startswith('.proof-' + recipe.digest + '-')):
        raise ValueError('working copy must be a per-run directory beside the cache entry')
    owned_directory(destination)
    if any(destination.iterdir()):
        raise ValueError('working copy directory must be empty')
    working = destination / 'root'
    working.mkdir()
    run(['unshare', '--map-auto', '--map-root-user', 'cp', '--archive', '--reflink=auto',
         '--', str(source) + '/.', str(working)])
    return working


def build(recipe, cache, temporary):
    cache = Path(cache).expanduser().absolute()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned_directory(cache)
    entry = cache / recipe.digest
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    with os.fdopen(os.open(cache / (recipe.digest + '.lock'), flags, 0o600), 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if entry.exists() or entry.is_symlink():
            return cached(recipe, cache)
        for command in ('mmdebstrap', 'unshare', 'newuidmap', 'newgidmap', 'tar', 'xz'):
            if shutil.which(command) is None:
                raise RuntimeError('missing required tool: ' + command + '; install mmdebstrap util-linux uidmap tar xz-utils')
        temporary = require_temporary(temporary)
        stage = Path(tempfile.mkdtemp(prefix='pie-root-', dir=temporary))
        stage.chmod(0o755)
        pending = None
        try:
            pending = Path(tempfile.mkdtemp(prefix='.' + recipe.digest + '-', dir=cache))
            (stage / 'keys').mkdir()
            (stage / 'keys').chmod(0o755)
            for name, data in recipe.keys.items():
                (stage / 'keys' / name).write_bytes(data)
                (stage / 'keys' / name).chmod(0o644)
            (stage / 'mm-tmp').mkdir()
            (stage / 'mm-tmp').chmod(0o1777)
            native, target = final_sources(recipe)
            for name, content in (('sources.list', native.encode()), ('ports.sources', target.encode()), ('recipe.json', recipe.canonical)):
                (stage / name).write_bytes(content)
                (stage / name).chmod(0o644)
            env = dict(os.environ, TMPDIR=str(stage / 'mm-tmp'))
            env.pop('APT_CONFIG', None)
            run(bootstrap_command(recipe, stage), env=env, cwd=stage)
            root = pending / 'root'
            root.mkdir()
            run(['unshare', '--map-auto', '--map-root-user', 'tar', '--numeric-owner', '-xpf',
                 str(stage / 'root.tar.xz'), '-C', str(root)])
            validate_root(recipe, root)
            (pending / 'recipe.json').write_bytes(recipe.canonical)
            (pending / 'complete.json').write_bytes(canonical({'schema': 1, 'recipe_sha256': recipe.digest, 'complete': True}))
            pending.rename(entry)
            pending = None
        finally:
            if pending is not None:
                remove_stage(pending)
            remove_stage(stage)
    return cached(recipe, cache)
