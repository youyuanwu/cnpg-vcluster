from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .config import parse_duration
from .files import write_private_file
from .process import run


class HostError(RuntimeError):
    pass


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
    state_path = _state_path(root)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HostError(f"malformed host state record: {state_path}") from exc
    else:
        state = {
            "max_user_instances": read_inotify("max_user_instances"),
            "max_user_watches": read_inotify("max_user_watches"),
        }
        write_private_file(state_path, json.dumps(state, sort_keys=True) + "\n")

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
    state_path = _state_path(root)
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        originals = {
            "max_user_instances": int(state["max_user_instances"]),
            "max_user_watches": int(state["max_user_watches"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HostError(f"malformed host state record: {state_path}") from exc

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
    try:
        state_path.parent.rmdir()
        state_path.parent.parent.rmdir()
    except OSError:
        pass
    print("restored recorded host inotify values")
