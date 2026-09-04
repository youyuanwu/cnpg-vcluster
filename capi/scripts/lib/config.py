from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DURATION_RE = re.compile(r"^([0-9]+)(s|m|h)$")


class ConfigError(ValueError):
    pass


def _parse_value(raw: str, line_number: int) -> str:
    try:
        parts = shlex.split(raw, comments=False, posix=True)
    except ValueError as exc:
        raise ConfigError(f"line {line_number}: {exc}") from exc
    if len(parts) != 1:
        raise ConfigError(f"line {line_number}: values containing spaces must be quoted")
    return parts[0]


def load_env_file(path: Path, *, overrides: dict[str, str] | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"unable to read configuration {path}: {exc}") from exc
    for line_number, source_line in enumerate(source.splitlines(), 1):
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{line_number}: expected KEY=value")
        key, raw = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            raise ConfigError(f"{path}:{line_number}: invalid key {key!r}")
        if key in values:
            raise ConfigError(f"{path}:{line_number}: duplicate key {key}")
        values[key] = _parse_value(raw.strip(), line_number)

    source = os.environ if overrides is None else overrides
    for key in values:
        if key in source:
            values[key] = source[key]
    return values


def load_configuration(root: Path) -> dict[str, str]:
    versions = load_env_file(root / "config" / "versions.env")
    settings = load_env_file(root / "config" / "settings.env")
    duplicates = versions.keys() & settings.keys()
    if duplicates:
        raise ConfigError(f"duplicate keys across configuration files: {sorted(duplicates)}")
    return versions | settings


def require(config: dict[str, str], *keys: str) -> None:
    missing = [key for key in keys if not config.get(key)]
    if missing:
        raise ConfigError(f"missing required configuration: {', '.join(missing)}")


def parse_duration(value: str) -> int:
    match = DURATION_RE.fullmatch(value)
    if not match:
        raise ConfigError(f"invalid duration {value!r}; expected integer followed by s, m, or h")
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    return amount * multiplier
