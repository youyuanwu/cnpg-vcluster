from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class IntegrityError(RuntimeError):
    pass


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private_file(path: Path, content: str | bytes) -> None:
    ensure_private_dir(path.parent)
    data = content.encode("utf-8") if isinstance(content, str) else content
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


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
