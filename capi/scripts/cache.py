from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from scripts.lib.config import parse_duration
from scripts.lib.files import (
    IntegrityError,
    ensure_private_dir,
    sha256_file,
    verify_sha256,
    write_private_file,
)
from scripts.lib.process import CommandError, run
from scripts.tools import AUTHORED_INPUTS, DOWNLOADS, TAG_SOURCES, acquire_tools, verify_all_inputs


CACHE_SCHEMA = 1
ACTIVE_SCHEMA = 1
IMAGE_PLATFORM = "linux/amd64"


def image_keys(config: dict[str, str]) -> tuple[str, ...]:
    keys = tuple(
        sorted(
            key
            for key, value in config.items()
            if key.endswith("_IMAGE")
            and not key.endswith("_IMAGE_TAGGED")
            and "@sha256:" in value
        )
    )
    if not keys:
        raise IntegrityError("no digest-pinned images are configured")
    for key in keys:
        if f"{key}_TAGGED" not in config:
            raise IntegrityError(f"{key} is missing provenance key {key}_TAGGED")
    return keys


def _repository_digest(reference: str) -> str:
    name, digest = reference.rsplit("@", 1)
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name.rsplit(":", 1)[0]
    return f"{name}@{digest}"


def _private_regular_file(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise IntegrityError(f"required cache file is missing: {path}") from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise IntegrityError(f"cache file is not an owner-only regular file: {path}")


def _requirements(config: dict[str, str]) -> dict[str, object]:
    inputs = [
        {"path": filename, "sha256": config[sha_key]}
        for filename, _, sha_key in DOWNLOADS
    ]
    inputs.extend(
        (
            {
                "path": f"cert-manager-{config['CERT_MANAGER_VERSION']}.tgz",
                "sha256": config["CERT_MANAGER_CHART_SHA256"],
            },
            {
                "path": f"cert-manager-{config['CERT_MANAGER_VERSION']}.digest",
                "value": config["CERT_MANAGER_OCI_DIGEST"],
            },
        )
    )
    authored = [
        {"path": relative, "sha256": config[checksum_key]}
        for relative, checksum_key in AUTHORED_INPUTS
    ]
    provenance = [
        {
            "repository": repository,
            "tag": config[version_key],
            "commit": config[commit_key],
        }
        for repository, version_key, commit_key in TAG_SOURCES
    ]
    images = [
        {
            "key": key,
            "tagged": config[f"{key}_TAGGED"],
            "digest": config[key],
            "platform": IMAGE_PLATFORM,
        }
        for key in image_keys(config)
    ]
    return {
        "inputs": inputs,
        "authoredInputs": authored,
        "provenance": provenance,
        "images": images,
    }


def _remote_image_digest(tagged: str, timeout: int) -> str:
    command = [
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        tagged,
        "--format",
        "{{json .Manifest}}",
    ]
    for attempt in range(4):
        try:
            result = run(command, timeout=timeout)
            return json.loads(result.stdout)["digest"]
        except CommandError as exc:
            transient = any(
                marker in exc.output.lower()
                for marker in (
                    "500 internal server error",
                    "502 bad gateway",
                    "503 service unavailable",
                    "504 gateway timeout",
                    "tls handshake timeout",
                    "connection reset",
                    "i/o timeout",
                    "unexpected eof",
                )
            )
            if not transient or attempt == 3:
                raise
            time.sleep(5)
    raise IntegrityError(f"image inspection produced no result: {tagged}")


def _require_host_digest(config: dict[str, str], key: str, timeout: int) -> None:
    result = run(
        [
            "docker",
            "image",
            "inspect",
            config[key],
            "--format",
            "{{json .RepoDigests}}",
        ],
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise IntegrityError(f"host image is missing exact digest reference: {key}")
    repo_digests = json.loads(result.stdout)
    expected = _repository_digest(config[key])
    if expected not in repo_digests:
        raise IntegrityError(
            f"host image {key} lacks exact RepoDigest {expected}"
        )


def _archive_image(
    config: dict[str, str],
    key: str,
    destination: Path,
    timeout: int,
) -> None:
    tagged = config[f"{key}_TAGGED"]
    exact = config[key]
    expected = exact.rsplit("@", 1)[1]
    actual = _remote_image_digest(tagged, timeout)
    if actual != expected:
        raise IntegrityError(f"{key}_TAGGED resolved to {actual}, expected {expected}")
    run(
        ["docker", "pull", "--platform", IMAGE_PLATFORM, exact],
        timeout=timeout,
    )
    _require_host_digest(config, key, timeout)
    run(["docker", "tag", exact, tagged], timeout=timeout)
    ensure_private_dir(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        run(
            ["docker", "image", "save", "--output", str(temporary), tagged, exact],
            timeout=timeout,
        )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    _verify_archive_metadata(destination, tagged, exact)


def _verify_archive_metadata(path: Path, tagged: str, exact: str) -> None:
    _private_regular_file(path)
    expected_digest = exact.rsplit("@", 1)[1]
    try:
        with tarfile.open(path, "r") as archive:
            index_member = archive.getmember("index.json")
            if not index_member.isfile():
                raise IntegrityError(f"image archive index is not a regular file: {path}")
            extracted = archive.extractfile(index_member)
            if extracted is None:
                raise IntegrityError(f"image archive index cannot be read: {path}")
            index = json.loads(extracted.read())
    except (KeyError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"invalid OCI image archive {path}: {exc}") from exc
    descriptors = index.get("manifests") or []
    if not any(item.get("digest") == expected_digest for item in descriptors):
        raise IntegrityError(
            f"image archive {path} does not contain source digest {expected_digest}"
        )
    if not any(
        (item.get("annotations") or {}).get("io.containerd.image.name") == tagged
        for item in descriptors
    ):
        raise IntegrityError(
            f"image archive {path} does not contain tagged reference {tagged}"
        )


def _generation_root(root: Path) -> Path:
    return root / ".tools" / "cache" / "generations"


def _active_path(root: Path) -> Path:
    return root / ".tools" / "cache" / "active.json"


def _load_json(path: Path) -> dict[str, object]:
    _private_regular_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IntegrityError(f"invalid JSON cache record {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise IntegrityError(f"cache record is not an object: {path}")
    return payload


def active_generation(root: Path) -> Path:
    active = _load_json(_active_path(root))
    if active.get("schema") != ACTIVE_SCHEMA:
        raise IntegrityError("unsupported cache active-record schema")
    generation = active.get("generation")
    if (
        not isinstance(generation, str)
        or not generation
        or "/" in generation
        or generation in {".", ".."}
    ):
        raise IntegrityError("invalid active cache generation")
    path = _generation_root(root) / generation
    if not path.is_dir() or path.is_symlink():
        raise IntegrityError(f"active cache generation is missing: {path}")
    ensure_private_dir(path)
    return path


def verify_generation(
    root: Path,
    config: dict[str, str],
    generation: Path,
) -> dict[str, object]:
    inventory_path = generation / "inventory.json"
    inventory = _load_json(inventory_path)
    if inventory.get("schema") != CACHE_SCHEMA:
        raise IntegrityError("unsupported cache inventory schema")
    if inventory.get("platform") != IMAGE_PLATFORM:
        raise IntegrityError("cache platform does not match linux/amd64")
    if inventory.get("requirements") != _requirements(config):
        raise IntegrityError("cache inventory does not match current pinned requirements")
    images = inventory.get("imageArchives")
    if not isinstance(images, list):
        raise IntegrityError("cache image archive inventory is missing")
    expected_keys = set(image_keys(config))
    observed_keys: set[str] = set()
    allowed = {"inventory.json"}
    for entry in images:
        if not isinstance(entry, dict):
            raise IntegrityError("cache image archive entry is not an object")
        key = entry.get("key")
        relative = entry.get("path")
        checksum = entry.get("sha256")
        if (
            not isinstance(key, str)
            or key not in expected_keys
            or key in observed_keys
            or not isinstance(relative, str)
            or not relative.startswith("images/")
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(checksum, str)
            or len(checksum) != 64
        ):
            raise IntegrityError("invalid cache image archive entry")
        observed_keys.add(key)
        allowed.add(relative)
        archive_path = generation / relative
        _private_regular_file(archive_path)
        verify_sha256(archive_path, checksum)
        _verify_archive_metadata(
            archive_path,
            config[f"{key}_TAGGED"],
            config[key],
        )
    if observed_keys != expected_keys:
        missing = sorted(expected_keys - observed_keys)
        raise IntegrityError(f"cache image inventory is incomplete: {missing}")
    for path in generation.rglob("*"):
        if path.is_dir():
            ensure_private_dir(path)
            continue
        relative = str(path.relative_to(generation))
        if relative not in allowed:
            raise IntegrityError(f"unexpected cache generation entry: {path}")
    verify_all_inputs(root, config)
    return inventory


def verify_cache(root: Path, config: dict[str, str]) -> dict[str, object]:
    return verify_generation(root, config, active_generation(root))


def archive_path(root: Path, config: dict[str, str], key: str) -> Path:
    generation = active_generation(root)
    inventory = verify_generation(root, config, generation)
    for entry in inventory["imageArchives"]:
        if entry["key"] == key:
            return generation / entry["path"]
    raise IntegrityError(f"cache image archive is missing: {key}")


def restore_host_image(
    root: Path,
    config: dict[str, str],
    key: str,
    timeout: int | None = None,
) -> None:
    effective_timeout = timeout or parse_duration(config["DOWNLOAD_TIMEOUT"])
    try:
        _require_host_digest(config, key, effective_timeout)
        return
    except IntegrityError:
        pass
    path = archive_path(root, config, key)
    run(
        ["docker", "image", "load", "--input", str(path)],
        timeout=effective_timeout,
    )
    _require_host_digest(config, key, effective_timeout)


def acquire_cache(root: Path, config: dict[str, str]) -> None:
    acquire_tools(root, config)
    timeout = parse_duration(config["DOWNLOAD_TIMEOUT"]) * 4
    generations = _generation_root(root)
    ensure_private_dir(generations)
    generation_id = uuid.uuid4().hex
    generation = generations / generation_id
    ensure_private_dir(generation)
    try:
        entries = []
        for key in image_keys(config):
            relative = f"images/{key.lower()}.tar"
            destination = generation / relative
            _archive_image(config, key, destination, timeout)
            entries.append(
                {
                    "key": key,
                    "path": relative,
                    "sha256": sha256_file(destination),
                }
            )
        inventory = {
            "schema": CACHE_SCHEMA,
            "platform": IMAGE_PLATFORM,
            "requirements": _requirements(config),
            "imageArchives": entries,
        }
        write_private_file(
            generation / "inventory.json",
            json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n",
        )
        verify_generation(root, config, generation)
        write_private_file(
            _active_path(root),
            json.dumps(
                {"schema": ACTIVE_SCHEMA, "generation": generation_id},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        verify_cache(root, config)
    except BaseException:
        shutil.rmtree(generation, ignore_errors=True)
        raise
    print(
        f"prepared verified cache generation {generation_id} "
        f"with {len(entries)} images"
    )
