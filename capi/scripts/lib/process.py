from __future__ import annotations

import subprocess
from dataclasses import dataclass
import os
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
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    effective = list(command)
    if os.environ.get("CAPI_OFFLINE_ENFORCED") == "1":
        blocked = (
            effective[:2] == ["git", "ls-remote"]
            or (len(effective) >= 2 and effective[0].endswith("/helm") and effective[1] == "pull")
            or effective[:2] == ["helm", "pull"]
            or effective[:3] == ["docker", "buildx", "imagetools"]
            or effective[:2] == ["docker", "pull"]
        )
        if blocked:
            raise CommandError(
                tuple(effective),
                125,
                "offline enforcement blocked network acquisition",
            )
        if effective[:2] == ["docker", "run"] and not any(
            argument.startswith("--pull") for argument in effective[2:]
        ):
            effective.insert(2, "--pull=never")
    try:
        result = subprocess.run(
            effective,
            cwd=cwd,
            env=env,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = f"{exc.stdout or ''}{exc.stderr or ''}"
        raise CommandError(tuple(effective), 124, redact(output)) from exc
    except OSError as exc:
        raise CommandError(tuple(effective), 126, str(exc)) from exc
    if check and result.returncode != 0:
        raise CommandError(tuple(effective), result.returncode, result.stdout + result.stderr)
    return result
