from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .redaction import redact, redact_argv


@dataclass
class CommandError(RuntimeError):
    command: tuple[str, ...]
    returncode: int
    output: str

    def __str__(self) -> str:
        command = redact_argv(self.command)
        return f"command failed ({self.returncode}): {command}\n{redact(self.output)}"


def run(
    command: Sequence[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = f"{exc.stdout or ''}{exc.stderr or ''}"
        raise CommandError(tuple(command), 124, redact(output)) from exc
    except OSError as exc:
        raise CommandError(tuple(command), 126, str(exc)) from exc
    if check and result.returncode != 0:
        raise CommandError(tuple(command), result.returncode, result.stdout + result.stderr)
    return result
