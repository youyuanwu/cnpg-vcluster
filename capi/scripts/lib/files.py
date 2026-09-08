from __future__ import annotations

import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class IntegrityError(RuntimeError):
    pass


def _private_path(path: Path) -> tuple[Path, tuple[str, ...]]:
    absolute = path.absolute()
    parts = absolute.parts
    for marker in (".runtime", ".tools"):
        if marker in parts:
            index = parts.index(marker)
            return Path(*parts[:index]), tuple(parts[index:])
    missing = []
    cursor = absolute
    while True:
        try:
            details = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor.name)
            cursor = cursor.parent
            continue
        if stat.S_ISLNK(details.st_mode):
            raise IntegrityError(f"private path parent is a symlink: {cursor}")
        if not stat.S_ISDIR(details.st_mode):
            raise IntegrityError(f"private path parent is not a directory: {cursor}")
        if not missing:
            return cursor.parent, (cursor.name,)
        return cursor, tuple(reversed(missing))


def _open_private_directory(parent_fd: int, name: str, display: Path) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise IntegrityError(
            f"unable to open private directory without following links: "
            f"{display}: {exc}"
        ) from exc
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        os.close(descriptor)
        raise IntegrityError(
            f"private directory is not an owned directory: {display}"
        )
    if details.st_mode & 0o077:
        os.close(descriptor)
        raise IntegrityError(
            f"private directory permissions are too broad: {display}"
        )
    return descriptor


@contextmanager
def private_directory(path: Path) -> Iterator[int]:
    base, components = _private_path(path)
    descriptors = []
    try:
        descriptor = os.open(
            base,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(descriptor)
        display = base
        for component in components:
            display /= component
            descriptor = _open_private_directory(
                descriptor, component, display
            )
            descriptors.append(descriptor)
        yield descriptors[-1]
    except OSError as exc:
        raise IntegrityError(
            f"unable to traverse private directory safely: {path}: {exc}"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def ensure_private_dir(path: Path) -> None:
    with private_directory(path):
        pass


def write_private_file(path: Path, content: str | bytes) -> None:
    data = content.encode("utf-8") if isinstance(content, str) else content
    temporary = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}"
    with private_directory(path.parent) as parent_fd:
        try:
            existing = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.getuid()
            or existing.st_mode & 0o077
        ):
            raise IntegrityError(
                f"private file is not an owner-only regular file: {path}"
            )
        descriptor = os.open(
            temporary,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            os.fsync(descriptor)
            os.replace(
                temporary,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    if not path.is_file():
        raise IntegrityError(f"required verified input is missing: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise IntegrityError(f"SHA-256 mismatch for {path}: expected {expected}, got {actual}")


def has_owner_only_permissions(path: Path) -> bool:
    return path.stat().st_mode & 0o077 == 0
