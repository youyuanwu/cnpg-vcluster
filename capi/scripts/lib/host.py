from __future__ import annotations

import json
import fcntl
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import parse_duration
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
def _host_lock(root: Path) -> Iterator[int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    runtime_fd = -1
    host_fd = -1
    lock_fd = -1
    try:
        runtime_fd = _open_private_directory(root_fd, ".runtime", root / ".runtime")
        host_fd = _open_private_directory(
            runtime_fd,
            "host",
            root / ".runtime" / "host",
        )
        lock_fd = os.open(
            ".lock",
            os.O_CREAT | os.O_RDWR | nofollow,
            0o600,
            dir_fd=host_fd,
        )
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
            raise HostError("host lock is not an owned regular file")
        if lock_stat.st_mode & 0o077:
            raise HostError("host lock permissions are broader than owner-only")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield host_fd
    except OSError as exc:
        raise HostError(f"unable to open host state without following links: {exc}") from exc
    finally:
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        remove_directories = host_fd >= 0 and not _entry_exists(host_fd, "inotify.json")
        if remove_directories:
            try:
                os.unlink(".lock", dir_fd=host_fd)
            except OSError:
                pass
        if host_fd >= 0:
            os.close(host_fd)
        if remove_directories and runtime_fd >= 0:
            try:
                os.rmdir("host", dir_fd=runtime_fd)
            except OSError:
                pass
        if runtime_fd >= 0:
            os.close(runtime_fd)
        if remove_directories:
            try:
                os.rmdir(".runtime", dir_fd=root_fd)
            except OSError:
                pass
        os.close(root_fd)


def _open_private_directory(parent_fd: int, name: str, display: Path) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        os.close(descriptor)
        raise HostError(f"private directory is not an owned directory: {display}")
    if details.st_mode & 0o077:
        os.close(descriptor)
        raise HostError(f"private directory permissions are too broad: {display}")
    return descriptor


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _validate_state_payload(payload: object, config: dict[str, str], path: Path) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise HostError(f"host state record is not an object: {path}")
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


def _read_state_record(directory_fd: int, path: Path, config: dict[str, str]) -> dict[str, int]:
    try:
        descriptor = os.open(
            "inotify.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise HostError(f"unable to open host state record safely: {path}: {exc}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise HostError(f"host state record is not an owned regular file: {path}")
        if details.st_mode & 0o077:
            raise HostError(f"host state record permissions are too broad: {path}")
        if details.st_size > 64 * 1024:
            raise HostError(f"host state record is unexpectedly large: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise HostError(f"malformed host state record: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _validate_state_payload(payload, config, path)


def _write_state_record(directory_fd: int, path: Path, payload: dict[str, object]) -> None:
    temporary = f".inotify.{os.getpid()}.{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
        os.rename(
            temporary,
            "inotify.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


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
    with _host_lock(root) as host_fd:
        state_path = _state_path(root)
        if _entry_exists(host_fd, "inotify.json"):
            _read_state_record(host_fd, state_path, config)
        else:
            state = {
                "schema": HOST_STATE_SCHEMA,
                "lab_prefix": config["LAB_PREFIX"],
                "ownership_label": config["OWNERSHIP_LABEL"],
                "owner_uid": os.getuid(),
                "max_user_instances": read_inotify("max_user_instances"),
                "max_user_watches": read_inotify("max_user_watches"),
            }
            _write_state_record(host_fd, state_path, state)
            _read_state_record(host_fd, state_path, config)

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
    with _host_lock(root) as host_fd:
        state_path = _state_path(root)
        if not _entry_exists(host_fd, "inotify.json"):
            return
        originals = _read_state_record(host_fd, state_path, config)

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
        os.unlink("inotify.json", dir_fd=host_fd)
        print("restored recorded host inotify values")
