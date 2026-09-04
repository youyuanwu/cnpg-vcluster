from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .files import ensure_private_dir


@contextmanager
def tools_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    tools_dir = root / ".tools"
    ensure_private_dir(tools_dir)
    lock_path = tools_dir / ".lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, mode)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
