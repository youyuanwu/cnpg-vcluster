from __future__ import annotations

import json
import hashlib
import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
CACHE_VERIFICATION_SCHEMA = 1
IMAGE_PLATFORM = "linux/amd64"


@dataclass(frozen=True)
class VerifiedCache:
    generation: Path
    inventory: dict[str, object]
    state_sha256: str


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


def _canonical_repo_digest(reference: str) -> str:
    name, digest = reference.rsplit("@", 1)
    if "/" not in name:
        name = f"docker.io/library/{name}"
    else:
        first = name.split("/", 1)[0]
        if "." not in first and ":" not in first and first != "localhost":
            name = f"docker.io/{name}"
    return f"{name}@{digest}"


def runtime_digest_reference(reference: str) -> str:
    return _canonical_repo_digest(_repository_digest(reference))


def canonical_exact_reference(reference: str) -> str:
    name, digest = reference.rsplit("@", 1)
    return f"{canonical_tagged(name)}@{digest}"


def canonical_tagged(reference: str) -> str:
    if "/" not in reference:
        return f"docker.io/library/{reference}"
    first = reference.split("/", 1)[0]
    if "." not in first and ":" not in first and first != "localhost":
        reference = f"docker.io/{reference}"
    return reference


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
    expected = runtime_digest_reference(config[key])
    if expected not in {_canonical_repo_digest(item) for item in repo_digests}:
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
    run(["docker", "pull", "--platform", IMAGE_PLATFORM, exact], timeout=timeout)
    _require_host_digest(config, key, timeout)
    run(["docker", "tag", exact, tagged], timeout=timeout)
    ensure_private_dir(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _write_registry_oci_archive(tagged, exact, temporary, timeout)
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    _verify_archive_metadata(destination, tagged, exact)


def _registry_coordinates(reference: str) -> tuple[str, str]:
    name = reference.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name.rsplit(":", 1)[0]
    if "/" not in name:
        return "registry-1.docker.io", f"library/{name}"
    first, remainder = name.split("/", 1)
    if "." in first or ":" in first or first == "localhost":
        registry = "registry-1.docker.io" if first == "docker.io" else first
        return registry, remainder
    return "registry-1.docker.io", name


def _registry_get(
    registry: str,
    repository: str,
    path: str,
    timeout: int,
    *,
    accept: str | None = None,
) -> bytes:
    url = f"https://{registry}/v2/{repository}/{path}"
    headers = {"User-Agent": "cnpg-vcluster-capi-lab"}
    if accept:
        headers["Accept"] = accept

    def request(extra: dict[str, str] | None = None):
        return urllib.request.urlopen(
            urllib.request.Request(url, headers=headers | (extra or {})),
            timeout=timeout,
        )

    try:
        with request() as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        challenge = exc.headers.get("WWW-Authenticate", "")
        if exc.code != 401 or not challenge.lower().startswith("bearer "):
            raise IntegrityError(f"registry request failed for {url}: {exc}") from exc
    parameters = dict(
        re.findall(r'([a-zA-Z]+)="([^"]*)"', challenge.removeprefix("Bearer "))
    )
    realm = parameters.get("realm")
    if not realm:
        raise IntegrityError(f"registry did not provide a bearer realm: {registry}")
    query = urllib.parse.urlencode(
        {
            key: value
            for key, value in (
                ("service", parameters.get("service")),
                ("scope", parameters.get("scope") or f"repository:{repository}:pull"),
            )
            if value
        }
    )
    token_url = f"{realm}?{query}" if query else realm
    with urllib.request.urlopen(
        urllib.request.Request(
            token_url, headers={"User-Agent": "cnpg-vcluster-capi-lab"}
        ),
        timeout=timeout,
    ) as response:
        token_payload = json.loads(response.read())
    token = token_payload.get("token") or token_payload.get("access_token")
    if not token:
        raise IntegrityError(f"registry token response was empty: {registry}")
    with request({"Authorization": f"Bearer {token}"}) as response:
        return response.read()


def _write_registry_oci_archive(
    tagged: str,
    exact: str,
    destination: Path,
    timeout: int,
) -> None:
    registry, repository = _registry_coordinates(exact)
    expected = exact.rsplit("@", 1)[1]
    manifest_accept = ", ".join(
        (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    )
    source_bytes = _registry_get(
        registry,
        repository,
        f"manifests/{expected}",
        timeout,
        accept=manifest_accept,
    )
    if hashlib.sha256(source_bytes).hexdigest() != expected.removeprefix("sha256:"):
        raise IntegrityError(f"registry source manifest digest mismatch: {exact}")
    source = json.loads(source_bytes)
    source_media_type = source.get("mediaType", "application/vnd.oci.image.index.v1+json")
    if "manifests" in source:
        platform = next(
            (
                item
                for item in source["manifests"]
                if (item.get("platform") or {}).get("os") == "linux"
                and (item.get("platform") or {}).get("architecture") == "amd64"
            ),
            None,
        )
        if platform is None:
            raise IntegrityError(f"registry image lacks linux/amd64: {exact}")
        platform_digest = platform["digest"]
        platform_bytes = _registry_get(
            registry,
            repository,
            f"manifests/{platform_digest}",
            timeout,
            accept=manifest_accept,
        )
    else:
        platform_digest = expected
        platform_bytes = source_bytes
    if hashlib.sha256(platform_bytes).hexdigest() != platform_digest.removeprefix("sha256:"):
        raise IntegrityError(f"registry platform manifest digest mismatch: {exact}")
    platform_manifest = json.loads(platform_bytes)
    blobs: dict[str, bytes] = {
        expected: source_bytes,
        platform_digest: platform_bytes,
    }
    for descriptor in [
        platform_manifest.get("config", {}),
        *platform_manifest.get("layers", []),
    ]:
        digest = descriptor.get("digest")
        if not digest:
            raise IntegrityError(f"registry platform manifest is incomplete: {exact}")
        data = _registry_get(
            registry,
            repository,
            f"blobs/{digest}",
            timeout,
        )
        if hashlib.sha256(data).hexdigest() != digest.removeprefix("sha256:"):
            raise IntegrityError(f"registry blob digest mismatch: {digest}")
        blobs[digest] = data
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": source_media_type,
                "digest": expected,
                "size": len(source_bytes),
                "annotations": {
                    "containerd.io/distribution.source." + registry: repository,
                    "io.containerd.image.name": canonical_tagged(tagged),
                    "org.opencontainers.image.ref.name": tagged.rsplit(":", 1)[-1],
                },
            }
        ],
    }
    entries = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}\n',
        "index.json": json.dumps(
            index, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n",
    }
    entries.update(
        {
            f"blobs/sha256/{digest.removeprefix('sha256:')}": data
            for digest, data in blobs.items()
        }
    )
    with tarfile.open(destination, "w") as archive:
        for directory in ("blobs", "blobs/sha256"):
            member = tarfile.TarInfo(directory)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            member.mtime = 0
            archive.addfile(member)
        for name in sorted(entries):
            data = entries[name]
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            member.mtime = 0
            archive.addfile(member, io.BytesIO(data))


def _verify_archive_metadata(path: Path, tagged: str, exact: str) -> None:
    _private_regular_file(path)
    expected_digest = exact.rsplit("@", 1)[1]
    try:
        with tarfile.open(path, "r") as archive:
            names = set(archive.getnames())

            def read_blob(digest: str) -> bytes:
                if not digest.startswith("sha256:") or len(digest) != 71:
                    raise IntegrityError(
                        f"image archive {path} has invalid blob digest {digest!r}"
                    )
                member_name = f"blobs/sha256/{digest.removeprefix('sha256:')}"
                if member_name not in names:
                    raise IntegrityError(
                        f"image archive {path} lacks content blob {digest}"
                    )
                extracted_blob = archive.extractfile(member_name)
                if extracted_blob is None:
                    raise IntegrityError(
                        f"image archive content blob cannot be read: {digest}"
                    )
                data = extracted_blob.read()
                if hashlib.sha256(data).hexdigest() != digest.removeprefix("sha256:"):
                    raise IntegrityError(
                        f"image archive content blob checksum mismatch: {digest}"
                    )
                return data

            index_member = archive.getmember("index.json")
            if not index_member.isfile():
                raise IntegrityError(f"image archive index is not a regular file: {path}")
            extracted = archive.extractfile(index_member)
            if extracted is None:
                raise IntegrityError(f"image archive index cannot be read: {path}")
            index = json.loads(extracted.read())
            source_manifest = json.loads(read_blob(expected_digest))
            platform = next(
                (
                    item
                    for item in source_manifest.get("manifests", [])
                    if (item.get("platform") or {}).get("os") == "linux"
                    and (item.get("platform") or {}).get("architecture") == "amd64"
                ),
                None,
            )
            if platform is None:
                raise IntegrityError(
                    f"image archive {path} lacks linux/amd64 platform metadata"
                )
            platform_digest = platform.get("digest", "")
            manifest = json.loads(read_blob(platform_digest))
            required_blobs = [manifest.get("config", {}).get("digest", "")]
            required_blobs.extend(
                layer.get("digest", "") for layer in manifest.get("layers", [])
            )
            for digest in required_blobs:
                read_blob(digest)
    except (KeyError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"invalid OCI image archive {path}: {exc}") from exc
    descriptors = index.get("manifests") or []
    if not any(item.get("digest") == expected_digest for item in descriptors):
        raise IntegrityError(
            f"image archive {path} does not contain source digest {expected_digest}"
        )
    if not any(
        canonical_tagged(
            (item.get("annotations") or {}).get("io.containerd.image.name", "")
        )
        == canonical_tagged(tagged)
        for item in descriptors
    ):
        raise IntegrityError(
            f"image archive {path} does not contain tagged reference {tagged}"
        )


def _generation_root(root: Path) -> Path:
    return root / ".tools" / "cache" / "generations"


def _active_path(root: Path) -> Path:
    return root / ".tools" / "cache" / "active.json"


def _verification_path(root: Path) -> Path:
    return root / ".tools" / "cache" / "verified.json"


def _load_json(path: Path) -> dict[str, object]:
    _private_regular_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IntegrityError(f"invalid JSON cache record {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise IntegrityError(f"cache record is not an object: {path}")
    return payload


def _requirements_sha256(requirements: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            requirements,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _metadata_entry(path: Path, name: str, *, private: bool) -> dict[str, object]:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise IntegrityError(f"required cache path is missing: {path}") from exc
    if stat.S_ISLNK(details.st_mode):
        raise IntegrityError(f"cache state path is a symlink: {path}")
    if private and (
        details.st_uid != os.getuid() or details.st_mode & 0o077
    ):
        raise IntegrityError(f"cache state path is not owner-only: {path}")
    if stat.S_ISDIR(details.st_mode):
        kind = "directory"
    elif stat.S_ISREG(details.st_mode):
        kind = "file"
    else:
        raise IntegrityError(f"cache state path has unsupported type: {path}")
    return {
        "path": name,
        "kind": kind,
        "device": details.st_dev,
        "inode": details.st_ino,
        "mode": stat.S_IMODE(details.st_mode),
        "size": details.st_size,
        "mtimeNs": details.st_mtime_ns,
        "ctimeNs": details.st_ctime_ns,
    }


def _cache_state_sha256(
    root: Path,
    generation: Path,
    requirements: dict[str, object],
) -> str:
    entries = [
        _metadata_entry(generation, ".", private=True),
        *(
            _metadata_entry(
                path,
                path.relative_to(generation).as_posix(),
                private=True,
            )
            for path in sorted(generation.rglob("*"))
        ),
    ]
    authored = requirements.get("authoredInputs")
    if not isinstance(authored, list):
        raise IntegrityError("cache authored-input requirements are missing")
    for item in authored:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise IntegrityError("cache authored-input requirement is invalid")
        relative = item["path"]
        entries.append(
            _metadata_entry(
                root / relative,
                f"authored:{relative}",
                private=False,
            )
        )
    return hashlib.sha256(
        json.dumps(
            {
                "generation": generation.name,
                "requirementsSha256": _requirements_sha256(requirements),
                "entries": entries,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _load_inventory_header(
    generation: Path,
    requirements: dict[str, object],
) -> dict[str, object]:
    inventory_path = generation / "inventory.json"
    inventory = _load_json(inventory_path)
    if inventory.get("schema") != CACHE_SCHEMA:
        raise IntegrityError("unsupported cache inventory schema")
    if inventory.get("platform") != IMAGE_PLATFORM:
        raise IntegrityError("cache platform does not match linux/amd64")
    if inventory.get("requirements") != requirements:
        raise IntegrityError("cache inventory does not match current pinned requirements")
    return inventory


def _verification_matches(
    root: Path,
    generation: Path,
    requirements_sha256: str,
    state_sha256: str,
) -> bool:
    path = _verification_path(root)
    if not path.exists() and not path.is_symlink():
        return False
    record = _load_json(path)
    return (
        record.get("schema") == CACHE_VERIFICATION_SCHEMA
        and record.get("generation") == generation.name
        and record.get("requirementsSha256") == requirements_sha256
        and record.get("stateSha256") == state_sha256
    )


def _write_verification(
    root: Path,
    generation: Path,
    requirements_sha256: str,
    state_sha256: str,
) -> None:
    write_private_file(
        _verification_path(root),
        json.dumps(
            {
                "schema": CACHE_VERIFICATION_SCHEMA,
                "generation": generation.name,
                "requirementsSha256": requirements_sha256,
                "stateSha256": state_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


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
    inputs_dir = generation / "inputs"
    verify_all_inputs(root, config, inputs_dir)
    for filename, _, _ in DOWNLOADS:
        allowed.add(f"inputs/{filename}")
    allowed.update(
        {
            f"inputs/cert-manager-{config['CERT_MANAGER_VERSION']}.tgz",
            f"inputs/cert-manager-{config['CERT_MANAGER_VERSION']}.digest",
        }
    )
    for path in generation.rglob("*"):
        if path.is_dir():
            ensure_private_dir(path)
            continue
        relative = str(path.relative_to(generation))
        if relative not in allowed:
            raise IntegrityError(f"unexpected cache generation entry: {path}")
    return inventory


def verify_cache(
    root: Path,
    config: dict[str, str],
    *,
    force: bool = False,
) -> VerifiedCache:
    generation = active_generation(root)
    requirements = _requirements(config)
    inventory = _load_inventory_header(generation, requirements)
    requirements_sha256 = _requirements_sha256(requirements)
    state_sha256 = _cache_state_sha256(root, generation, requirements)
    if (
        not force
        and _verification_matches(
            root,
            generation,
            requirements_sha256,
            state_sha256,
        )
    ):
        return VerifiedCache(generation, inventory, state_sha256)
    inventory = verify_generation(root, config, generation)
    verified_state_sha256 = _cache_state_sha256(
        root,
        generation,
        requirements,
    )
    if verified_state_sha256 != state_sha256:
        raise IntegrityError("cache generation changed during verification")
    _write_verification(
        root,
        generation,
        requirements_sha256,
        verified_state_sha256,
    )
    return VerifiedCache(generation, inventory, verified_state_sha256)


def archive_path(
    root: Path,
    config: dict[str, str],
    key: str,
    *,
    verified: VerifiedCache | None = None,
) -> Path:
    cache = verified or verify_cache(root, config)
    for entry in cache.inventory["imageArchives"]:
        if entry["key"] == key:
            return cache.generation / entry["path"]
    raise IntegrityError(f"cache image archive is missing: {key}")


def restore_host_image(
    root: Path,
    config: dict[str, str],
    key: str,
    timeout: int | None = None,
    *,
    verified: VerifiedCache | None = None,
) -> None:
    effective_timeout = timeout or parse_duration(config["DOWNLOAD_TIMEOUT"])
    try:
        _require_host_digest(config, key, effective_timeout)
        return
    except IntegrityError:
        pass
    path = archive_path(root, config, key, verified=verified)
    run(
        ["docker", "image", "load", "--input", str(path)],
        timeout=effective_timeout,
    )
    _require_host_digest(config, key, effective_timeout)


def _copy_private(source: Path, destination: Path) -> None:
    _private_regular_file(source)
    ensure_private_dir(destination.parent)
    if destination.exists() or destination.is_symlink():
        _private_regular_file(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_file, temporary.open("wb") as output:
            shutil.copyfileobj(input_file, output)
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def materialize_inputs(
    root: Path,
    config: dict[str, str],
    *,
    verified: VerifiedCache | None = None,
) -> None:
    cache = verified or verify_cache(root, config)
    source_dir = cache.generation / "inputs"
    destination_dir = root / ".tools" / "inputs"
    ensure_private_dir(destination_dir)
    allowed = {
        filename for filename, _, _ in DOWNLOADS
    } | {
        f"cert-manager-{config['CERT_MANAGER_VERSION']}.tgz",
        f"cert-manager-{config['CERT_MANAGER_VERSION']}.digest",
    }
    for name in sorted(allowed):
        _copy_private(source_dir / name, destination_dir / name)
    for path in destination_dir.iterdir():
        if path.name in allowed:
            continue
        _private_regular_file(path)
        path.unlink()
    verify_all_inputs(root, config)


def acquire_cache(root: Path, config: dict[str, str]) -> None:
    timeout = parse_duration(config["DOWNLOAD_TIMEOUT"]) * 4
    generations = _generation_root(root)
    ensure_private_dir(generations)
    generation_id = uuid.uuid4().hex
    generation = generations / generation_id
    ensure_private_dir(generation)
    published = False
    try:
        acquire_tools(root, config, tools_dir=generation)
        shutil.rmtree(generation / "bin")
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
        published = True
        verified = verify_cache(root, config, force=True)
        materialize_inputs(root, config, verified=verified)
    except BaseException:
        if not published:
            shutil.rmtree(generation, ignore_errors=True)
        raise
    print(
        f"prepared verified cache generation {generation_id} "
        f"with {len(entries)} images"
    )
