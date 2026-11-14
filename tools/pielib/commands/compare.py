# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
"""usage: pie compare OLD NEW --mode bytes|package|tree [--out DIR] [--json]

bytes requires the same nonempty set of .deb filenames and SHA-256 values.
Both archives are validated, even for identical debs, as in pie-verify.
package pairs directories by Package and Architecture, rejecting duplicates.
Two explicit debs are compared directly, including their package identities.
All control fields and both archives' member metadata and file hashes are
reported. debdiff is required. diffoscope is optional and its absence is shown.
Packages are copied and validated before diagnostics. Only control files
are unpacked. Data members and link targets are inventoried without extraction.
Diagnostic tools read the validated deb copies, never an unpacked tree.
tree accepts two refs in the current repository or two directory snapshots.
Directory snapshots include ignored and untracked files, omit .git entries
and empty directories, and use git file modes. Input repositories are not changed.
With --out, writes summary.txt and the full report.json to DIR.
Existing report files are refused. Other files in DIR are left in place.
Prints the text summary, or the full report with --json, to standard output.
With --out, diagnostic stderr is also saved under DIR/diagnostics/.
Exit status: 0 identical, 1 different, 2 invalid input, resource or diagnostic failure.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

from pielib.deb import sha256, staged_deb, validated_sha256
from pielib.deb822 import control_paragraphs
from pielib.environment import fixed_environment


def deb_paths(value):
    path = Path(value).absolute()
    if path.is_dir():
        paths = sorted(path.glob('*.deb'))
    elif path.is_file() and path.suffix == '.deb':
        paths = [path]
    else:
        raise ValueError('%s is not a deb or a directory of debs' % value)
    if not paths:
        raise ValueError('%s contains no debs' % value)
    for item in paths:
        try:
            item.name.encode('utf-8')
        except UnicodeError as error:
            raise ValueError('non-UTF-8 deb filename: %s' % ascii(str(item))) from error
    if any(not p.is_file() for p in paths):
        raise ValueError('%s contains a .deb entry that is not a file' % value)
    return paths


def compare_bytes(old, new):
    a = {p.name: validated_sha256(p) for p in deb_paths(old)}
    b = {p.name: validated_sha256(p) for p in deb_paths(new)}
    missing = sorted(a.keys() - b.keys())
    extra = sorted(b.keys() - a.keys())
    different = sorted(n for n in a.keys() & b.keys() if a[n] != b[n])
    same = sorted(n for n in a.keys() & b.keys() if a[n] == b[n])
    report = dict(mode='bytes', old=a, new=b, missing=missing, extra=extra,
                  different=different, identical=same)
    lines = ['bytes: %d identical, %d missing, %d extra, %d different' %
             (len(same), len(missing), len(extra), len(different))]
    for label, names in [('missing', missing), ('extra', extra), ('different', different)]:
        lines.extend('%s: %s' % (label, n) for n in names)
    return report, lines, int(bool(missing or extra or different))


def package_snapshot(path, workspace):
    staged, control, raw_control, data, ar = staged_deb(path, workspace)
    if raw_control is None:
        raise ValueError('%s has no regular control file' % path)
    paragraphs = control_paragraphs(raw_control)
    if len(paragraphs) != 1 or not all(paragraphs[0].get(k) for k in ('Package', 'Architecture')):
        raise ValueError('%s needs one control paragraph with Package and Architecture' % path)
    return dict(path=str(path), filename=path.name, sha256=sha256(staged), validated_path=str(staged),
                fields=dict(paragraphs[0]), raw_control=raw_control, control=control, data=data, ar=ar)


def changed_values(a, b):
    return {name: dict(old=a.get(name), new=b.get(name))
            for name in sorted(a.keys() | b.keys()) if a.get(name) != b.get(name)}


def diagnostic(command, optional=False):
    if shutil.which(command[0]) is None:
        return dict(command=command, status='unavailable', stdout='',
                    stderr='%s is not installed' % command[0], error=not optional)
    result = subprocess.run(command, capture_output=True, text=True, errors='backslashreplace')
    return dict(command=command, status='completed' if result.returncode in (0, 1) else 'failed',
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                error=result.returncode not in (0, 1))


def package_pair(a, b):
    fields = changed_values(a['fields'], b['fields'])
    control = changed_values(a['control'], b['control'])
    data = changed_values(a['data'], b['data'])
    ar = changed_values({str(m['position']): m for m in a['ar']},
                        {str(m['position']): m for m in b['ar']})
    different = a['sha256'] != b['sha256'] or a['filename'] != b['filename']
    pair = dict(old=a, new=b, different=different, fields=fields, control=control, data=data, ar=ar,
                diagnostics={})
    if different:
        pair['diagnostics']['debdiff'] = diagnostic(
            ['debdiff', '--no-conf', '--dirs', '--control', '--controlfiles', 'ALL', a['validated_path'], b['validated_path']])
        pair['diagnostics']['diffoscope'] = diagnostic(
            ['diffoscope', '--text', '-', '--text-color', 'never', '--no-progress',
             '--no-default-limits', '--force-details', a['validated_path'], b['validated_path']], optional=True)
    return pair


def package_index(paths, workspace):
    out = {}
    for path in paths:
        snapshot = package_snapshot(path, workspace)
        key = (snapshot['fields']['Package'], snapshot['fields']['Architecture'])
        if key in out:
            raise ValueError('ambiguous package %s [%s]: %s and %s' %
                             (*key, out[key]['path'], path))
        out[key] = snapshot
    return out


def listed_names(names):
    first = ', '.join(json.dumps(name, ensure_ascii=True) for name in names[:20])
    return first + ('; %d more in report.json' % (len(names) - 20) if len(names) > 20 else '')


def diagnostic_error(result):
    lines = result.get('stderr', '').strip().splitlines()
    return (lines[-1].strip()[:300] if lines else 'exit %s' % result.get('returncode', 'unknown'))


def package_report(old, new, workspace):
    a, b = deb_paths(old), deb_paths(new)
    if Path(old).is_file() and Path(new).is_file():
        pairs = [package_pair(package_snapshot(a[0], workspace), package_snapshot(b[0], workspace))]
        missing, extra = [], []
    else:
        ai, bi = package_index(a, workspace), package_index(b, workspace)
        pairs = [package_pair(ai[k], bi[k]) for k in sorted(ai.keys() & bi.keys())]
        missing = [ai[k] for k in sorted(ai.keys() - bi.keys())]
        extra = [bi[k] for k in sorted(bi.keys() - ai.keys())]
    different = len(missing) + len(extra) + sum(p['different'] for p in pairs)
    lines = ['package: %d paired, %d missing, %d extra, %d different pairs' %
             (len(pairs), len(missing), len(extra), sum(p['different'] for p in pairs))]
    for label, snapshots in [('missing', missing), ('extra', extra)]:
        lines.extend('%s: %s' % (label, s['filename']) for s in snapshots)
    failed = False
    for pair in pairs:
        fields = pair['new']['fields']
        lines.append('%s [%s]: %s; control fields: %s; control members: %d; data members: %d' %
                     (fields['Package'], fields['Architecture'], 'different' if pair['different'] else 'identical',
                      ', '.join(pair['fields']) or 'none', len(pair['control']), len(pair['data'])))
        for part in ('control', 'data', 'ar'):
            if pair[part]:
                names = list(pair[part]) if part != 'ar' else [
                    (delta['new'] or delta['old'])['name'] for delta in pair[part].values()]
                lines.append('%s members: %s' % (part, listed_names(names)))
        if pair['old']['filename'] != pair['new']['filename']:
            lines.append('filename: %s -> %s' % (pair['old']['filename'], pair['new']['filename']))
        if pair['old']['sha256'] != pair['new']['sha256'] and not any(pair[p] for p in ('fields', 'control', 'data', 'ar')):
            lines.append('archive bytes differ; no itemised difference explains the change')
        for name, result in pair['diagnostics'].items():
            if result['status'] != 'completed':
                lines.append('%s: %s: %s' % (name, result['status'], diagnostic_error(result)))
            failed |= result['error']
    return dict(mode='package', pairs=pairs, missing=missing, extra=extra), lines, 2 if failed else int(bool(different))


def compare_packages(old, new):
    with tempfile.TemporaryDirectory(prefix='pie-compare-') as workspace:
        return package_report(old, new, workspace)


def git(args, **kwargs):
    return subprocess.run(['git', *args], check=True, capture_output=True, **kwargs).stdout


def directory_entries(path, blobs, scratch):
    entries = []
    for child in sorted(path.iterdir()):
        if child.name == '.git':
            continue
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            body = directory_entries(child, blobs, scratch)
            if not body:
                continue
            mode, kind = '040000', 'tree'
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            mode, kind = ('120000' if stat.S_ISLNK(info.st_mode) else
                          '100755' if info.st_mode & 0o100 else '100644'), 'blob'
            body = len(blobs)
            if stat.S_ISLNK(info.st_mode):
                content = scratch / ('link-%d' % body)
                content.write_bytes(os.fsencode(os.readlink(child)))
            else:
                content = child.absolute()
            blobs.append(content)
        else:
            raise ValueError('%s is not a regular file, symlink or directory' % child)
        entries.append((child.name, mode, kind, body))
    return entries


def quoted_git_path(path):
    parts = []
    for value in os.fsencode(path):
        if value in (34, 92) or value < 32 or value >= 127:
            parts.append(('\\%03o' % value).encode())
        else:
            parts.append(bytes([value]))
    return b'"' + b''.join(parts) + b'"\n'


def directory_tree(entries, hashes, repository):
    records = []
    for name, mode, kind, body in entries:
        oid = directory_tree(body, hashes, repository) if kind == 'tree' else hashes[body]
        records.append(mode.encode() + b' ' + kind.encode() + b' ' + oid + b'\t' + os.fsencode(name) + b'\0')
    return git(['--git-dir', repository, 'mktree', '-z'], input=b''.join(records)).strip()


def tree_report(old, new, prefix, a, b):
    a, b = a.decode(), b.decode()
    diffstat = git([*prefix, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                    '--stat=80', '--summary', a, b, '--'], text=True)
    lines = ['tree: %s' % ('identical' if a == b else 'different'),
             'old: %s' % a, 'new: %s' % b, diffstat.rstrip() or 'no changes']
    return dict(mode='tree', old=dict(input=old, tree=a), new=dict(input=new, tree=b),
                stat=diffstat), lines, int(a != b)


def compare_trees(old, new):
    dirs = Path(old).is_dir(), Path(new).is_dir()
    if dirs == (True, True):
        inputs = [Path(value).resolve() for value in (old, new)]
        scratch = next((root for root in (tempfile.gettempdir(), '/tmp')
                        if not any(Path(root).resolve().is_relative_to(path) for path in inputs)), None)
        if scratch is None:
            raise ValueError('tree snapshots need a temporary directory outside both inputs; set TMPDIR')
        with tempfile.TemporaryDirectory(prefix='pie-compare-', dir=scratch) as tmp:
            repository = str(Path(tmp) / 'objects.git')
            git(['init', '--bare', '--quiet', repository])
            blobs = []
            entries = [directory_entries(Path(value), blobs, Path(tmp)) for value in (old, new)]
            hashes = (git(['--git-dir', repository, 'hash-object', '-w', '--stdin-paths', '--no-filters'],
                          input=b''.join(quoted_git_path(p) for p in blobs)).splitlines() if blobs else [])
            a, b = [directory_tree(tree, hashes, repository) for tree in entries]
            return tree_report(old, new, ['--git-dir', repository], a, b)
    if any(dirs):
        raise ValueError('tree mode needs two directories or two refs')
    a, b = [git(['rev-parse', '--verify', '--end-of-options', ref + '^{tree}']).strip() for ref in (old, new)]
    return tree_report(old, new, [], a, b)


def check_output(output):
    if output is not None:
        if output.exists() and not output.is_dir():
            raise ValueError('--out must name a directory: %s' % output)
        for name in ('summary.txt', 'report.json', 'diagnostics'):
            if os.path.lexists(output / name):
                raise ValueError('report already exists: %s' % (output / name))


def write_report(output, report, lines, status, as_json=False):
    summary = '\n'.join(lines) + '\n'
    full = json.dumps(dict(report, status=status), indent=2, sort_keys=True) + '\n'
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        with (output / 'summary.txt').open('x', encoding='utf-8', errors='surrogateescape') as stream:
            stream.write(summary)
        with (output / 'report.json').open('x', encoding='utf-8', errors='surrogateescape') as stream:
            stream.write(full)
        for index, pair in enumerate(report.get('pairs', [])):
            for name, result in pair['diagnostics'].items():
                if result.get('stderr'):
                    logs = output / 'diagnostics'
                    logs.mkdir(exist_ok=True)
                    with (logs / ('%03d-%s.log' % (index, name))).open(
                            'x', encoding='utf-8', errors='surrogateescape') as stream:
                        stream.write(result['stderr'])
    if as_json:
        print(full, end='')
    else:
        rendered = summary.encode('utf-8', errors='backslashreplace').decode('utf-8')
        print(rendered, end='', file=sys.stderr if 'error' in report else sys.stdout)


def main():
    fixed_environment()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('old')
    parser.add_argument('new')
    parser.add_argument('--mode', required=True, choices=('bytes', 'package', 'tree'))
    parser.add_argument('--out', type=Path)
    parser.add_argument('--json', action='store_true', help='print the full JSON report instead of text')
    args = parser.parse_args()
    try:
        check_output(args.out)
    except (OSError, ValueError) as error:
        print('pie compare: %s' % error, file=sys.stderr)
        sys.exit(2)
    try:
        report, lines, status = dict(bytes=compare_bytes, package=compare_packages,
                                     tree=compare_trees)[args.mode](args.old, args.new)
    except (MemoryError, OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        if isinstance(error, MemoryError):
            detail = 'resource failure: insufficient memory'
        else:
            detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
            if isinstance(detail, bytes):
                detail = detail.decode(errors='backslashreplace')
        report, lines, status = dict(mode=args.mode, error=detail), ['pie compare: %s' % detail], 2
    try:
        write_report(args.out, report, lines, status, args.json)
    except (MemoryError, OSError, UnicodeError) as error:
        detail = 'resource failure: insufficient memory' if isinstance(error, MemoryError) else str(error)
        print('pie compare: cannot write report: %s' % detail, file=sys.stderr)
        sys.exit(2)
    sys.exit(status)
