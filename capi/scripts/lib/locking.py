from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .files import (
    IntegrityError,
    ensure_private_dir,
    existing_private_directory,
    private_file_exists,
    private_directory,
)


def _lock_descriptor(path: Path, *, create: bool) -> int | None:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        ensure_private_dir(path.parent)
        flags |= os.O_CREAT
        directory = private_directory(path.parent)
    else:
        try:
            directory = existing_private_directory(path.parent)
        except IntegrityError:
            raise
    try:
        with directory as parent_fd:
            try:
                descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
    except IntegrityError as exc:
        if not create and isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        os.close(descriptor)
        raise RuntimeError(f"lock is not an owner-only regular file: {path}")
    return descriptor


def profile_lock_path(root: Path, profile: str) -> Path:
    if profile not in {"local", "azure"}:
        raise RuntimeError(f"unsupported tenant profile lock: {profile}")
    return root / ".runtime" / "lifecycle" / ".locks" / f"{profile}.lock"


def profile_lock_exists(root: Path, profile: str) -> bool:
    return private_file_exists(profile_lock_path(root, profile))


@contextmanager
def profile_lock(
    root: Path,
    profile: str,
    *,
    exclusive: bool,
    create: bool,
) -> Iterator[bool]:
    descriptor = _lock_descriptor(
        profile_lock_path(root, profile),
        create=create,
    )
    if descriptor is None:
        yield False
        return
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield True
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def tools_lock(
    root: Path,
    *,
    exclusive: bool,
    create: bool = True,
) -> Iterator[bool]:
    lock_path = root / ".tools" / ".lock"
    descriptor = _lock_descriptor(lock_path, create=create)
    if descriptor is None:
        yield False
        return
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, mode)
        yield True
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def e2e_lock(
    root: Path,
    *,
    exclusive: bool,
    create: bool = True,
) -> Iterator[bool]:
    lock_path = root / ".tools" / ".e2e.lock"
    descriptor = _lock_descriptor(lock_path, create=create)
    if descriptor is None:
        yield False
        return
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield True
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
