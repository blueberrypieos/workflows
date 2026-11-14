# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 The Blueberry Pie OS Authors
from contextlib import contextmanager
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile


CHUNK_SIZE = 1024 * 1024


class StrictTarInfo(tarfile.TarInfo):
    @classmethod
    def fromtarfile(cls, archive):
        try:
            return super().fromtarfile(archive)
        except (tarfile.InvalidHeaderError, tarfile.TruncatedHeaderError) as error:
            raise tarfile.SubsequentHeaderError(str(error)) from error


def sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def member_name(name):
    return (name[2:] if name.startswith('./') else name).rstrip('/') or '.'


def checked_members(archive):
    seen, aliases = {}, set()
    for member in archive:
        name = member_name(member.name)
        canonical = str(PurePosixPath(member.name))
        if name in seen or canonical in aliases:
            raise ValueError('%s occurs twice' % name)
        if member.islnk() and seen.get(member_name(member.linkname)) not in (tarfile.REGTYPE, tarfile.AREGTYPE):
            raise ValueError('%s is a hard link to %s, which is not a regular file before it' % (name, member.linkname))
        seen[name] = member.type
        aliases.add(canonical)
        body = archive.extractfile(member) if member.isfile() else None
        try:
            yield name, member, body
        finally:
            if body is not None:
                body.close()
        archive.members.clear()
    while chunk := archive.fileobj.read(CHUNK_SIZE):
        if chunk.strip(b'\0'):
            raise ValueError('non-zero bytes after the tar end marker')


def finish_archive(process, errors, command):
    while process.stdout.read(CHUNK_SIZE):
        pass
    status = process.wait()
    if status:
        errors.seek(0)
        raise subprocess.CalledProcessError(status, command, stderr=errors.read())


@contextmanager
def archive_members(deb, part):
    command = ['dpkg-deb', '--fsys-tarfile' if part == 'data' else '--ctrl-tarfile', str(deb)]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            try:
                with tarfile.open(fileobj=process.stdout, mode='r|', tarinfo=StrictTarInfo) as archive:
                    yield checked_members(archive)
            except tarfile.TarError as error:
                finish_archive(process, errors, command)
                raise ValueError('%s: %s archive: %s' % (deb, part, error)) from error
            except ValueError as error:
                raise ValueError('%s: %s archive: %s' % (deb, part, error)) from error
            finish_archive(process, errors, command)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def deb_members(deb, part):
    out = {}
    with archive_members(deb, part) as members:
        for name, m, body in members:
            out[name] = (m.type, m.mode, m.uid, m.gid, m.uname, m.gname, m.linkname, m.mtime,
                         m.devmajor, m.devminor, tuple(sorted(m.pax_headers.items())), m.name,
                         body.read() if body is not None else None)
    return out


def validated_sha256(deb):
    try:
        for part in ('data', 'control'):
            with archive_members(deb, part) as members:
                for name, member, body in members:
                    if body is not None:
                        hashlib.file_digest(body, 'sha256')
    except subprocess.CalledProcessError as error:
        raise ValueError('%s: %s' % (deb, error.stderr.decode(errors='backslashreplace').strip())) from error
    return sha256(deb)


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('unsafe archive path: %s' % name)
    return str(path)


def archive_inventory(deb, part, root=None):
    if part == 'control':
        root.mkdir(mode=0o700)
    records, raw_control = {}, None
    with archive_members(deb, part) as members:
        for position, (name, member, body) in enumerate(members):
            destination = None
            if part == 'control':
                safe = safe_name(member.name)
                if not member.isfile() and not (safe == '.' and member.isdir()):
                    raise ValueError('unsafe control member: %s must be a regular file or the root directory' % name)
                destination = root / safe
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            size = digest = None
            text = (bytearray() if part == 'control' and safe == 'control' and body is not None else None)
            if body is not None:
                hasher = hashlib.sha256()
                output = destination.open('xb') if destination is not None else None
                try:
                    while chunk := body.read(CHUNK_SIZE):
                        hasher.update(chunk)
                        if output is not None:
                            output.write(chunk)
                        if text is not None:
                            text.extend(chunk)
                finally:
                    if output is not None:
                        output.close()
                size, digest = member.size, hasher.hexdigest()
            elif member.islnk():
                record = records[member_name(member.linkname)]
                size, digest = record['size'], record['sha256']
            if text is not None:
                raw_control = text.decode('utf-8', errors='surrogateescape')
            records[name] = dict(raw_name=member.name, position=position,
                                 type=member.type.decode('ascii'), mode='%04o' % member.mode,
                                 uid=member.uid, gid=member.gid, uname=member.uname, gname=member.gname,
                                 link=member.linkname, mtime=member.mtime, devmajor=member.devmajor,
                                 devminor=member.devminor, pax_headers=dict(member.pax_headers),
                                 size=size, sha256=digest)
    return records, raw_control


def ar_members(deb):
    members = []
    with open(deb, 'rb') as stream:
        if stream.read(8) != b'!<arch>\n':
            raise ValueError('not a Debian ar archive')
        while header := stream.read(60):
            if len(header) != 60 or header[-2:] != b'`\n':
                raise ValueError('invalid ar member header')
            size = int(header[48:58])
            if size < 0:
                raise ValueError('negative ar member size')
            remaining, hasher = size, hashlib.sha256()
            while remaining:
                chunk = stream.read(min(remaining, CHUNK_SIZE))
                if not chunk:
                    raise ValueError('truncated ar member')
                remaining -= len(chunk)
                hasher.update(chunk)
            padding = stream.read(size % 2)
            if len(padding) != size % 2:
                raise ValueError('missing ar padding')
            members.append(dict(name=header[:16].rstrip(b' /').decode('utf-8', errors='surrogateescape'),
                                position=len(members), size=size, sha256=hasher.hexdigest(),
                                raw_header=header.hex(), padding=padding.hex()))
    return members


def staged_deb(path, workspace):
    directory = Path(tempfile.mkdtemp(prefix='deb-', dir=workspace))
    staged = directory / path.name
    try:
        shutil.copyfile(path, staged)
        control, raw_control = archive_inventory(staged, 'control', directory / 'control')
        data, _ = archive_inventory(staged, 'data')
        ar = ar_members(staged)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors='backslashreplace').strip().replace(str(staged), str(path))
        raise ValueError('%s: %s' % (path, detail)) from error
    except (OSError, ValueError) as error:
        detail = str(error).replace(str(staged), str(path))
        raise ValueError(detail if detail.startswith(str(path) + ':') else '%s: %s' % (path, detail)) from error
    return staged, control, raw_control, data, ar
