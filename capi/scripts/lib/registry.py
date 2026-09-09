from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from scripts.cache import (
    _registry_coordinates,
    active_generation,
    archive_path,
    canonical_tagged,
    verify_cache,
)
from scripts.lib.files import IntegrityError, ensure_private_dir, write_private_file
from scripts.lib.ownership import OwnershipError
from scripts.lib.process import run


REGISTRY_SCHEMA = 1
REGISTRY_PORT = 5000
REGISTRY_ROLE = "offline-registry"
MIRRORED_IMAGE_KEYS = (
    "KUBE_APISERVER_IMAGE",
    "KUBE_CONTROLLER_MANAGER_IMAGE",
    "KUBE_SCHEDULER_IMAGE",
    "KONNECTIVITY_SERVER_IMAGE",
    "CNPG_CONTROLLER_IMAGE",
)


@dataclass(frozen=True)
class MirrorImage:
    key: str
    registry: str
    repository: str
    tag: str
    source_digest: str
    source_media_type: str
    platform_digest: str
    platform_media_type: str
    content: dict[str, bytes]


def _record_path(root: Path) -> Path:
    return root / ".runtime" / "management" / "offline-registry.json"


def _data_path(root: Path) -> Path:
    return root / ".runtime" / "management" / "offline-registry-data"


def _hosts_path(root: Path) -> Path:
    return root / ".runtime" / "rendered" / "registry-hosts.toml"


def registry_name(config: dict[str, str]) -> str:
    return f"{config['LAB_PREFIX']}-offline-registry"


def _private_file(path: Path) -> None:
    details = path.lstat()
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise OwnershipError(f"registry file is not an owner-only regular file: {path}")


def _private_dir(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise OwnershipError(f"registry directory is missing: {path}") from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise OwnershipError(f"registry directory is not owner-only: {path}")


def _path_present(path: Path) -> bool:
    return os.path.lexists(path)


def _read_blob(archive: tarfile.TarFile, digest: str) -> bytes:
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise IntegrityError(f"invalid OCI digest in registry mirror: {digest!r}")
    member = archive.getmember(f"blobs/sha256/{digest.removeprefix('sha256:')}")
    if not member.isfile():
        raise IntegrityError(f"registry mirror blob is not a regular file: {digest}")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise IntegrityError(f"registry mirror blob cannot be read: {digest}")
    data = extracted.read()
    if hashlib.sha256(data).hexdigest() != digest.removeprefix("sha256:"):
        raise IntegrityError(f"registry mirror blob checksum mismatch: {digest}")
    return data


def load_mirror_image(
    archive_file: Path,
    key: str,
    tagged: str,
    exact: str,
) -> MirrorImage:
    registry, repository = _registry_coordinates(exact)
    canonical = canonical_tagged(tagged)
    image_name, tag = canonical.rsplit(":", 1)
    canonical_registry = image_name.split("/", 1)[0]
    request_registry = (
        "registry-1.docker.io" if canonical_registry == "docker.io" else canonical_registry
    )
    if request_registry != registry or image_name != f"{canonical_registry}/{repository}":
        raise IntegrityError(f"offline mirror tag does not match exact image: {key}")
    source_digest = exact.rsplit("@", 1)[1]
    try:
        with tarfile.open(archive_file, "r") as archive:
            source_bytes = _read_blob(archive, source_digest)
            source = json.loads(source_bytes)
            platform = next(
                (
                    descriptor
                    for descriptor in source.get("manifests", [])
                    if (descriptor.get("platform") or {}).get("os") == "linux"
                    and (descriptor.get("platform") or {}).get("architecture")
                    == "amd64"
                ),
                None,
            )
            if platform is None:
                raise IntegrityError(f"offline mirror image lacks linux/amd64: {key}")
            platform_digest = platform.get("digest", "")
            platform_bytes = _read_blob(archive, platform_digest)
            manifest = json.loads(platform_bytes)
            content = {
                source_digest: source_bytes,
                platform_digest: platform_bytes,
            }
            for descriptor in [
                manifest.get("config", {}),
                *manifest.get("layers", []),
            ]:
                digest = descriptor.get("digest", "")
                content[digest] = _read_blob(archive, digest)
    except (KeyError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"invalid offline registry archive {archive_file}: {exc}") from exc
    return MirrorImage(
        key=key,
        registry=canonical_registry,
        repository=repository,
        tag=tag,
        source_digest=source_digest,
        source_media_type=source.get(
            "mediaType", "application/vnd.oci.image.index.v1+json"
        ),
        platform_digest=platform_digest,
        platform_media_type=manifest.get(
            "mediaType", "application/vnd.oci.image.manifest.v1+json"
        ),
        content=content,
    )


def _blob_path(data_root: Path, digest: str) -> Path:
    value = digest.removeprefix("sha256:")
    return (
        data_root
        / "docker"
        / "registry"
        / "v2"
        / "blobs"
        / "sha256"
        / value[:2]
        / value
        / "data"
    )


def _repository_root(data_root: Path, repository: str) -> Path:
    return data_root / "docker" / "registry" / "v2" / "repositories" / repository


def _link(path: Path, digest: str) -> None:
    write_private_file(path, digest)


def _write_image(data_root: Path, image: MirrorImage) -> None:
    repository_root = _repository_root(data_root, image.repository)
    for digest, data in image.content.items():
        path = _blob_path(data_root, digest)
        if path.exists():
            _private_file(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest.removeprefix(
                "sha256:"
            ):
                raise IntegrityError(f"shared registry blob conflicts: {digest}")
        else:
            write_private_file(path, data)
    manifest_digests = (image.source_digest, image.platform_digest)
    for digest in manifest_digests:
        _link(
            repository_root
            / "_manifests"
            / "revisions"
            / "sha256"
            / digest.removeprefix("sha256:")
            / "link",
            digest,
        )
    for digest in image.content:
        if digest not in manifest_digests:
            _link(
                repository_root
                / "_layers"
                / "sha256"
                / digest.removeprefix("sha256:")
                / "link",
                digest,
            )
    tag_root = repository_root / "_manifests" / "tags" / image.tag
    _link(tag_root / "current" / "link", image.source_digest)
    _link(
        tag_root
        / "index"
        / "sha256"
        / image.source_digest.removeprefix("sha256:")
        / "link",
        image.source_digest,
    )


def _tree_inventory(data_root: Path) -> dict[str, str]:
    _private_dir(data_root)
    inventory: dict[str, str] = {}
    for path in sorted(data_root.rglob("*")):
        relative = path.relative_to(data_root).as_posix()
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or details.st_uid != os.getuid():
            raise OwnershipError(f"registry data path is not owned: {relative}")
        if details.st_mode & 0o077:
            raise OwnershipError(f"registry data path is not owner-only: {relative}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(details.st_mode):
            raise OwnershipError(f"registry data path is not regular: {relative}")
        inventory[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return inventory


def build_registry_data(
    root: Path,
    config: dict[str, str],
) -> tuple[list[MirrorImage], dict[str, str], str]:
    inventory = verify_cache(root, config).inventory
    generation = (root / ".tools" / "cache" / "active.json")
    generation_name = json.loads(generation.read_text(encoding="utf-8"))["generation"]
    data_root = _data_path(root)
    if _path_present(data_root):
        raise OwnershipError("offline registry data exists without a reusable record")
    ensure_private_dir(data_root)
    images: list[MirrorImage] = []
    coordinates: dict[tuple[str, str], str] = {}
    try:
        for key in MIRRORED_IMAGE_KEYS:
            image = load_mirror_image(
                archive_path(root, config, key),
                key,
                config[f"{key}_TAGGED"],
                config[key],
            )
            coordinate = (image.repository, image.tag)
            previous = coordinates.setdefault(coordinate, image.source_digest)
            if previous != image.source_digest:
                raise IntegrityError(
                    f"offline mirror repository collision: {image.repository}:{image.tag}"
                )
            _write_image(data_root, image)
            images.append(image)
        files = _tree_inventory(data_root)
        if not files:
            raise IntegrityError("offline registry data inventory is empty")
    except BaseException:
        shutil.rmtree(data_root, ignore_errors=True)
        raise
    if inventory.get("platform") != "linux/amd64":
        raise IntegrityError("offline registry cache platform changed unexpectedly")
    return images, files, str(generation_name)


def _inspect_container(name: str) -> dict[str, object] | None:
    response = run(["docker", "inspect", name], timeout=30, check=False)
    if response.returncode != 0:
        return None
    payload = json.loads(response.stdout)
    if len(payload) != 1:
        raise OwnershipError(f"unexpected registry container inventory: {name}")
    return payload[0]


def _validate_container(
    config: dict[str, str],
    record: dict[str, object],
    payload: dict[str, object],
) -> None:
    labels = (payload.get("Config") or {}).get("Labels") or {}
    expected_labels = {
        config["OWNERSHIP_LABEL"]: config["LAB_PREFIX"],
        "cnpg-vcluster.capi/role": REGISTRY_ROLE,
    }
    observed = {key: labels.get(key, "") for key in expected_labels}
    if (
        record.get("name") != registry_name(config)
        or record.get("identifier") != payload.get("Id")
        or record.get("imageIdentifier") != payload.get("Image")
        or record.get("imageReference") != config["OFFLINE_REGISTRY_IMAGE"]
        or record.get("labels") != expected_labels
        or observed != expected_labels
    ):
        raise OwnershipError("offline registry container ownership mismatch")
    networks = (payload.get("NetworkSettings") or {}).get("Networks") or {}
    if set(networks) != {record.get("network")}:
        raise OwnershipError("offline registry network attachment mismatch")
    address = networks[record["network"]].get("IPAddress")
    if not address or address != record.get("address"):
        raise OwnershipError("offline registry address mismatch")


def _load_record(root: Path) -> dict[str, object]:
    path = _record_path(root)
    _private_file(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OwnershipError("offline registry record is malformed") from exc
    if not isinstance(record, dict) or record.get("schema") != REGISTRY_SCHEMA:
        raise OwnershipError("offline registry record schema is invalid")
    return record


def validate_registry_state(root: Path, config: dict[str, str]) -> dict[str, object]:
    record = _load_record(root)
    payload = _inspect_container(registry_name(config))
    if payload is None:
        raise OwnershipError("offline registry record exists without its container")
    _validate_container(config, record, payload)
    files = record.get("files")
    if not isinstance(files, dict) or _tree_inventory(_data_path(root)) != files:
        raise OwnershipError("offline registry data inventory mismatch")
    return record


def _expected_images(config: dict[str, str]) -> list[dict[str, str]]:
    expected = []
    for key in MIRRORED_IMAGE_KEYS:
        registry, repository = _registry_coordinates(config[key])
        tagged = canonical_tagged(config[f"{key}_TAGGED"])
        image_name, tag = tagged.rsplit(":", 1)
        canonical_registry = image_name.split("/", 1)[0]
        request_registry = (
            "registry-1.docker.io"
            if canonical_registry == "docker.io"
            else canonical_registry
        )
        if request_registry != registry or image_name != f"{canonical_registry}/{repository}":
            raise IntegrityError(f"offline registry configuration mismatch: {key}")
        expected.append(
            {
                "key": key,
                "registry": canonical_registry,
                "repository": repository,
                "tag": tag,
                "sourceDigest": config[key].rsplit("@", 1)[1],
            }
        )
    return expected


def _registry_get(address: str, path: str, *, accept: str | None = None) -> tuple[bytes, str]:
    headers = {"User-Agent": "cnpg-vcluster-capi-offline-mirror"}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(
        f"http://{address}:{REGISTRY_PORT}{path}",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read(), response.headers.get("Docker-Content-Digest", "")


def _wait_registry(address: str) -> None:
    for _ in range(30):
        try:
            _registry_get(address, "/v2/")
            return
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    raise RuntimeError("offline registry did not become ready")


def _verify_registry(address: str, images: list[MirrorImage]) -> None:
    _wait_registry(address)
    manifest_accept = ", ".join(
        (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    )
    for image in images:
        for reference, expected_digest in (
            (image.tag, image.source_digest),
            (image.source_digest, image.source_digest),
            (image.platform_digest, image.platform_digest),
        ):
            data, header_digest = _registry_get(
                address,
                f"/v2/{image.repository}/manifests/{reference}",
                accept=manifest_accept,
            )
            actual = "sha256:" + hashlib.sha256(data).hexdigest()
            if actual != expected_digest or header_digest != expected_digest:
                raise IntegrityError(
                    f"offline registry manifest identity mismatch: {image.key}"
                )
        for digest, expected in image.content.items():
            if digest in {image.source_digest, image.platform_digest}:
                continue
            data, header_digest = _registry_get(
                address,
                f"/v2/{image.repository}/blobs/{digest}",
            )
            if data != expected or (
                header_digest and header_digest != digest
            ):
                raise IntegrityError(
                    f"offline registry blob identity mismatch: {image.key} {digest}"
                )


def _validate_reusable_registry_record(
    root: Path,
    config: dict[str, str],
    network: dict[str, object],
) -> dict[str, object]:
    record = validate_registry_state(root, config)
    verify_cache(root, config)
    observed_images = [
        {
            key: image.get(key)
            for key in ("key", "registry", "repository", "tag", "sourceDigest")
        }
        for image in record.get("images", [])
        if isinstance(image, dict)
    ]
    if (
        record.get("network") != network.get("network")
        or record.get("networkIdentifier") != network.get("network_id")
        or record.get("generation") != active_generation(root).name
        or observed_images != _expected_images(config)
    ):
        raise OwnershipError("offline registry cache or network identity changed")
    return record


def validate_retained_offline_registry(
    root: Path,
    config: dict[str, str],
    management_container: str,
    network: dict[str, object],
) -> None:
    if os.environ.get("CAPI_OFFLINE_ENFORCED") != "1":
        return
    _validate_reusable_registry_record(root, config, network)
    verify_offline_registry_pulls(config, management_container)


def _configure_containerd(
    root: Path,
    config: dict[str, str],
    management_container: str,
    address: str,
) -> None:
    registries = sorted({item["registry"] for item in _expected_images(config)})
    for registry in registries:
        server = (
            "https://registry-1.docker.io"
            if registry == "docker.io"
            else f"https://{registry}"
        )
        content = (
            f'server = "{server}"\n\n'
            f'[host."http://{address}:{REGISTRY_PORT}"]\n'
            '  capabilities = ["pull", "resolve"]\n'
        )
        hosts = _hosts_path(root).with_name(f"registry-hosts-{registry}.toml")
        write_private_file(hosts, content)
        destination_dir = f"/etc/containerd/certs.d/{registry}"
        run(
            ["docker", "exec", management_container, "mkdir", "-p", destination_dir],
            timeout=30,
        )
        run(
            [
                "docker",
                "cp",
                str(hosts),
                f"{management_container}:{destination_dir}/hosts.toml",
            ],
            timeout=30,
        )
        run(
            [
                "docker",
                "exec",
                management_container,
                "chmod",
                "600",
                f"{destination_dir}/hosts.toml",
            ],
            timeout=30,
        )


def reconcile_offline_registry(
    root: Path,
    config: dict[str, str],
    management_container: str,
    network: dict[str, object],
) -> dict[str, object] | None:
    if os.environ.get("CAPI_OFFLINE_ENFORCED") != "1":
        return None
    name = registry_name(config)
    record_path = _record_path(root)
    existing = _inspect_container(name)
    record_present = _path_present(record_path)
    data_present = _path_present(_data_path(root))
    if existing is not None or record_present or data_present:
        if existing is None or not record_present or not data_present:
            raise OwnershipError("offline registry state is partial or unowned")
        _private_dir(_data_path(root))
        record = _validate_reusable_registry_record(root, config, network)
        _configure_containerd(
            root, config, management_container, str(record["address"])
        )
        return record

    images, files, generation = build_registry_data(root, config)
    labels = {
        config["OWNERSHIP_LABEL"]: config["LAB_PREFIX"],
        "cnpg-vcluster.capi/role": REGISTRY_ROLE,
    }
    command = [
        "docker",
        "run",
        "--pull=never",
        "--detach",
        "--name",
        name,
        "--network",
        str(network["network"]),
        "--mount",
        f"type=bind,src={_data_path(root)},dst=/var/lib/registry,readonly",
    ]
    for key, value in labels.items():
        command.extend(("--label", f"{key}={value}"))
    command.append(config["OFFLINE_REGISTRY_IMAGE"])
    try:
        run(command, timeout=60)
        payload = _inspect_container(name)
        if payload is None:
            raise RuntimeError("offline registry container disappeared during startup")
        address = (
            (payload.get("NetworkSettings") or {})
            .get("Networks", {})
            .get(network["network"], {})
            .get("IPAddress")
        )
        if not address:
            raise RuntimeError("offline registry has no management-network address")
        record = {
            "schema": REGISTRY_SCHEMA,
            "name": name,
            "identifier": payload["Id"],
            "imageIdentifier": payload["Image"],
            "imageReference": config["OFFLINE_REGISTRY_IMAGE"],
            "labels": labels,
            "network": network["network"],
            "networkIdentifier": network["network_id"],
            "address": address,
            "generation": generation,
            "files": files,
            "images": [
                {
                    "key": image.key,
                    "registry": image.registry,
                    "repository": image.repository,
                    "tag": image.tag,
                    "sourceDigest": image.source_digest,
                    "platformDigest": image.platform_digest,
                }
                for image in images
            ],
        }
        write_private_file(
            record_path,
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
        )
        validate_registry_state(root, config)
        _verify_registry(address, images)
        _configure_containerd(root, config, management_container, address)
        return record
    except BaseException:
        payload = _inspect_container(name)
        if payload is not None:
            run(["docker", "rm", "--force", name], timeout=30, check=False)
        record_path.unlink(missing_ok=True)
        for hosts in _hosts_path(root).parent.glob("registry-hosts-*.toml"):
            hosts.unlink(missing_ok=True)
        shutil.rmtree(_data_path(root), ignore_errors=True)
        raise


def configure_offline_registry_node(
    root: Path,
    config: dict[str, str],
    container: str,
) -> None:
    if os.environ.get("CAPI_OFFLINE_ENFORCED") != "1":
        return
    record = validate_registry_state(root, config)
    _configure_containerd(root, config, container, str(record["address"]))


def verify_offline_registry_pulls(
    config: dict[str, str],
    management_container: str,
    keys: tuple[str, ...] = MIRRORED_IMAGE_KEYS,
) -> None:
    if os.environ.get("CAPI_OFFLINE_ENFORCED") != "1":
        return
    for key in keys:
        run(
            [
                "docker",
                "exec",
                management_container,
                "crictl",
                "pull",
                config[key],
            ],
            timeout=120,
        )
        print(
            "CAPI_OFFLINE_MIRROR "
            + json.dumps(
                {
                    "container": management_container,
                    "image": config[key],
                    "key": key,
                    "schema": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def delete_offline_registry(root: Path, config: dict[str, str]) -> None:
    name = registry_name(config)
    record_path = _record_path(root)
    payload = _inspect_container(name)
    record_present = _path_present(record_path)
    data_present = _path_present(_data_path(root))
    state_present = payload is not None or record_present or data_present
    if not state_present:
        for hosts in _hosts_path(root).parent.glob("registry-hosts-*.toml"):
            hosts.unlink(missing_ok=True)
        return
    if payload is None or not record_present or not data_present:
        raise OwnershipError("offline registry cleanup refused partial state")
    _private_dir(_data_path(root))
    validate_registry_state(root, config)
    run(["docker", "rm", "--force", name], timeout=30)
    if _inspect_container(name) is not None:
        raise RuntimeError("offline registry container remained after removal")
    validate_registry_state_files(root)
    shutil.rmtree(_data_path(root))
    record_path.unlink()
    for hosts in _hosts_path(root).parent.glob("registry-hosts-*.toml"):
        hosts.unlink(missing_ok=True)


def validate_registry_state_files(root: Path) -> None:
    record = _load_record(root)
    files = record.get("files")
    if not isinstance(files, dict) or _tree_inventory(_data_path(root)) != files:
        raise OwnershipError("offline registry files changed before cleanup")
