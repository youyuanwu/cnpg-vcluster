from __future__ import annotations

import json
import fcntl
import os
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import parse_duration
from .files import ensure_private_dir, has_owner_only_permissions, write_private_file
from .process import run


class HostError(RuntimeError):
    pass


HOST_STATE_SCHEMA = 1


def read_inotify(name: str) -> int:
    path = Path("/proc/sys/fs/inotify") / name
    value = path.read_text(encoding="utf-8").strip()
    if not value.isdigit():
        raise HostError(f"invalid inotify value in {path}: {value!r}")
    return int(value)


def resolve_host_just(root: Path, config: dict[str, str]) -> Path:
    executable = shutil.which("just")
    if executable is None:
        raise HostError("host-installed just is required")
    path = Path(executable).resolve()
    tools_bin = (root / ".tools" / "bin").resolve()
    if path == tools_bin or tools_bin in path.parents:
        raise HostError("just resolving inside .tools/bin is not a host prerequisite")
    result = run([str(path), "--version"], timeout=30)
    expected = f"just {config['JUST_VERSION']}"
    if result.stdout.strip() != expected:
        raise HostError(f"expected {expected}, got {result.stdout.strip()!r}")
    return path


def _state_path(root: Path) -> Path:
    return root / ".runtime" / "host" / "inotify.json"


@contextmanager
def _host_lock(root: Path) -> Iterator[None]:
    host_dir = root / ".runtime" / "host"
    ensure_private_dir(host_dir)
    lock_path = host_dir / ".lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise HostError(f"unable to open host lock {lock_path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HostError(f"host lock is not a regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        if not _state_path(root).exists():
            lock_path.unlink(missing_ok=True)
            try:
                host_dir.rmdir()
                host_dir.parent.rmdir()
            except OSError:
                pass


def _validate_state_record(path: Path, config: dict[str, str]) -> dict[str, int]:
    if path.is_symlink() or not path.is_file():
        raise HostError(f"host state record is not a regular file: {path}")
    if not has_owner_only_permissions(path) or not has_owner_only_permissions(path.parent):
        raise HostError(f"host state path is not owner-only: {path}")
    if path.stat().st_uid != os.getuid() or path.parent.stat().st_uid != os.getuid():
        raise HostError(f"host state path belongs to another user: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostError(f"malformed host state record: {path}") from exc
    expected_keys = {
        "schema",
        "lab_prefix",
        "ownership_label",
        "owner_uid",
        "max_user_instances",
        "max_user_watches",
    }
    if set(payload) != expected_keys:
        raise HostError(f"host state record has unexpected fields: {path}")
    if payload["schema"] != HOST_STATE_SCHEMA:
        raise HostError(f"unsupported host state schema in {path}")
    if payload["lab_prefix"] != config["LAB_PREFIX"]:
        raise HostError(f"host state record belongs to another lab: {path}")
    if payload["ownership_label"] != config["OWNERSHIP_LABEL"]:
        raise HostError(f"host state record ownership label mismatch: {path}")
    if payload["owner_uid"] != os.getuid():
        raise HostError(f"host state record belongs to another user: {path}")
    try:
        values = {
            "max_user_instances": int(payload["max_user_instances"]),
            "max_user_watches": int(payload["max_user_watches"]),
        }
    except (TypeError, ValueError) as exc:
        raise HostError(f"host state record values are invalid: {path}") from exc
    if any(value < 0 for value in values.values()):
        raise HostError(f"host state record values are invalid: {path}")
    return values


def _apply_sysctl(root: Path, config: dict[str, str], name: str, value: int) -> None:
    timeout = parse_duration(config["COMMAND_TIMEOUT"])
    sudo = shutil.which("sudo")
    if sudo:
        probe = run([sudo, "-n", "true"], timeout=30, check=False)
        if probe.returncode == 0:
            run([sudo, "sysctl", "-q", "-w", f"fs.inotify.{name}={value}"], timeout=timeout)
            return
    run(
        [
            "docker",
            "run",
            "--rm",
            "--privileged",
            "--pid=host",
            "--network=host",
            "--entrypoint",
            "sh",
            config["KIND_NODE_IMAGE"],
            "-ec",
            f"sysctl -q -w fs.inotify.{name}={value}",
        ],
        timeout=timeout,
    )


def prepare_inotify(root: Path, config: dict[str, str]) -> None:
    with _host_lock(root):
        state_path = _state_path(root)
        if state_path.exists() or state_path.is_symlink():
            _validate_state_record(state_path, config)
        else:
            state = {
                "schema": HOST_STATE_SCHEMA,
                "lab_prefix": config["LAB_PREFIX"],
                "ownership_label": config["OWNERSHIP_LABEL"],
                "owner_uid": os.getuid(),
                "max_user_instances": read_inotify("max_user_instances"),
                "max_user_watches": read_inotify("max_user_watches"),
            }
            write_private_file(state_path, json.dumps(state, sort_keys=True) + "\n")
            _validate_state_record(state_path, config)

        floors = {
            "max_user_instances": int(config["MIN_INOTIFY_INSTANCES"]),
            "max_user_watches": int(config["MIN_INOTIFY_WATCHES"]),
        }
        for name, floor in floors.items():
            if read_inotify(name) < floor:
                _apply_sysctl(root, config, name, floor)
            if read_inotify(name) < floor:
                raise HostError(f"fs.inotify.{name} remains below required floor {floor}")
        print(f"host inotify values prepared; originals recorded in {state_path}")


def restore_inotify(root: Path, config: dict[str, str]) -> None:
    with _host_lock(root):
        state_path = _state_path(root)
        if not state_path.exists() and not state_path.is_symlink():
            return
        originals = _validate_state_record(state_path, config)

        owned_workers = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label={config['OWNERSHIP_LABEL']}=true",
                "--filter",
                "label=cnpg-vcluster.capi/role=worker",
            ],
            timeout=30,
        ).stdout.split()
        if owned_workers:
            raise HostError("cannot restore host inotify values while owned workers exist")

        for name, value in originals.items():
            _apply_sysctl(root, config, name, value)
            if read_inotify(name) != value:
                raise HostError(f"failed to restore fs.inotify.{name}")
        state_path.unlink()
        print("restored recorded host inotify values")
