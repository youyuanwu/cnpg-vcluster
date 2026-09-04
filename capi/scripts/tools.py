from __future__ import annotations

import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from scripts.lib.config import parse_duration, require
from scripts.lib.files import IntegrityError, ensure_private_dir, verify_sha256
from scripts.lib.process import run


DOWNLOADS = (
    ("kind-linux-amd64", "KIND_URL", "KIND_SHA256"),
    ("kubectl-linux-amd64", "KUBECTL_URL", "KUBECTL_SHA256"),
    ("helm-linux-amd64.tar.gz", "HELM_URL", "HELM_SHA256"),
    ("clusterctl-linux-amd64", "CLUSTERCTL_URL", "CLUSTERCTL_SHA256"),
    ("capi-core-components.yaml", "CAPI_CORE_COMPONENTS_URL", "CAPI_CORE_COMPONENTS_SHA256"),
    ("capi-bootstrap-components.yaml", "CAPI_BOOTSTRAP_COMPONENTS_URL", "CAPI_BOOTSTRAP_COMPONENTS_SHA256"),
    ("capd-components.yaml", "CAPI_CAPD_COMPONENTS_URL", "CAPI_CAPD_COMPONENTS_SHA256"),
    ("capi-metadata.yaml", "CAPI_METADATA_URL", "CAPI_METADATA_SHA256"),
    ("kamaji-capi-components.yaml", "KAMAJI_CAPI_COMPONENTS_URL", "KAMAJI_CAPI_COMPONENTS_SHA256"),
    ("kamaji-capi-metadata.yaml", "KAMAJI_CAPI_METADATA_URL", "KAMAJI_CAPI_METADATA_SHA256"),
    ("kamaji-source.tar.gz", "KAMAJI_SOURCE_URL", "KAMAJI_SOURCE_SHA256"),
    ("kamaji-etcd-0.15.0.tgz", "KAMAJI_ETCD_CHART_URL", "KAMAJI_ETCD_CHART_SHA256"),
    ("metallb-native.yaml", "METALLB_MANIFEST_URL", "METALLB_MANIFEST_SHA256"),
    ("calico.yaml", "CALICO_MANIFEST_URL", "CALICO_MANIFEST_SHA256"),
    ("nfs-subdir-external-provisioner-4.0.18.tgz", "NFS_PROVISIONER_CHART_URL", "NFS_PROVISIONER_CHART_SHA256"),
    ("cnpg.yaml", "CNPG_MANIFEST_URL", "CNPG_MANIFEST_SHA256"),
)

EXPECTED_MANIFEST_IMAGES = {
    "capi-core-components.yaml": "CAPI_CORE_IMAGE_TAGGED",
    "capi-bootstrap-components.yaml": "CAPI_BOOTSTRAP_IMAGE_TAGGED",
    "capd-components.yaml": "CAPD_IMAGE_TAGGED",
    "kamaji-capi-components.yaml": "KAMAJI_CAPI_IMAGE_TAGGED",
}

TAG_SOURCES = (
    ("https://github.com/kubernetes-sigs/cluster-api.git", "CAPI_VERSION", "CAPI_TAG_COMMIT"),
    (
        "https://github.com/clastix/cluster-api-control-plane-provider-kamaji.git",
        "KAMAJI_CAPI_VERSION",
        "KAMAJI_CAPI_TAG_COMMIT",
    ),
    ("https://github.com/clastix/kamaji.git", "KAMAJI_VERSION", "KAMAJI_TAG_COMMIT"),
    ("https://github.com/ehough/docker-nfs-server.git", "NFS_SERVER_VERSION", "NFS_SERVER_TAG_COMMIT"),
    (
        "https://github.com/kubernetes-sigs/nfs-subdir-external-provisioner.git",
        "NFS_PROVISIONER_CHART_VERSION",
        "NFS_PROVISIONER_CHART_TAG_COMMIT",
    ),
)


def _download(url: str, destination: Path, timeout: int) -> None:
    ensure_private_dir(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "cnpg-vcluster-capi-lab"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IntegrityError(f"failed to download {url}: {exc}") from exc
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_download(
    inputs_dir: Path,
    filename: str,
    url: str,
    expected_sha256: str,
    timeout: int,
) -> Path:
    destination = inputs_dir / filename
    if destination.exists():
        try:
            verify_sha256(destination, expected_sha256)
            return destination
        except IntegrityError:
            destination.unlink()
    _download(url, destination, timeout)
    verify_sha256(destination, expected_sha256)
    return destination


def _install_copy(source: Path, destination: Path) -> None:
    ensure_private_dir(destination.parent)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        with source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
    try:
        temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _install_helm(archive: Path, destination: Path, expected_binary_sha256: str) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember("linux-amd64/helm")
        if not member.isfile():
            raise IntegrityError("Helm archive does not contain a regular linux-amd64/helm file")
        extracted = bundle.extractfile(member)
        if extracted is None:
            raise IntegrityError("unable to extract Helm binary")
        ensure_private_dir(destination.parent)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            shutil.copyfileobj(extracted, output)
    try:
        temporary.chmod(0o755)
        verify_sha256(temporary, expected_binary_sha256)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_tag(repository: str, tag: str, expected_commit: str, timeout: int) -> None:
    result = run(
        ["git", "ls-remote", repository, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        timeout=timeout,
    )
    commits = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    if expected_commit not in commits:
        raise IntegrityError(
            f"tag {tag} from {repository} did not resolve to expected commit {expected_commit}"
        )


def _verify_metadata(path: Path, minor: str, contract: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(?m)^\s*-\s+major:\s+[01]\s*$\n\s+minor:\s+{re.escape(minor)}\s*$\n\s+contract:\s+{re.escape(contract)}\s*$"
    )
    if not pattern.search(text):
        raise IntegrityError(f"{path} does not advertise minor {minor} contract {contract}")


def _ensure_cert_manager_chart(
    config: dict[str, str],
    inputs_dir: Path,
    bin_dir: Path,
    timeout: int,
) -> Path:
    destination = inputs_dir / f"cert-manager-{config['CERT_MANAGER_VERSION']}.tgz"
    if destination.exists():
        try:
            verify_sha256(destination, config["CERT_MANAGER_CHART_SHA256"])
            return destination
        except IntegrityError:
            destination.unlink()

    with tempfile.TemporaryDirectory(dir=inputs_dir) as temporary_dir:
        run(
            [
                str(bin_dir / "helm"),
                "pull",
                config["CERT_MANAGER_OCI"],
                "--version",
                config["CERT_MANAGER_VERSION"],
                "--destination",
                temporary_dir,
            ],
            timeout=timeout,
        )
        candidates = list(Path(temporary_dir).glob("cert-manager-*.tgz"))
        if len(candidates) != 1:
            raise IntegrityError("Helm did not produce exactly one cert-manager chart")
        shutil.move(candidates[0], destination)
        destination.chmod(0o600)
    verify_sha256(destination, config["CERT_MANAGER_CHART_SHA256"])
    return destination


def verify_all_inputs(root: Path, config: dict[str, str]) -> None:
    inputs_dir = root / ".tools" / "inputs"
    for filename, _, sha_key in DOWNLOADS:
        verify_sha256(inputs_dir / filename, config[sha_key])
    verify_sha256(
        inputs_dir / f"cert-manager-{config['CERT_MANAGER_VERSION']}.tgz",
        config["CERT_MANAGER_CHART_SHA256"],
    )

    _verify_metadata(
        inputs_dir / "capi-metadata.yaml",
        config["CAPI_VERSION"].removeprefix("v").split(".")[1],
        config["CAPI_CONTRACT"],
    )
    _verify_metadata(
        inputs_dir / "kamaji-capi-metadata.yaml",
        config["KAMAJI_CAPI_VERSION"].removeprefix("v").split(".")[1],
        config["KAMAJI_CAPI_CONTRACT"],
    )
    for filename, image_key in EXPECTED_MANIFEST_IMAGES.items():
        text = (inputs_dir / filename).read_text(encoding="utf-8")
        if text.count(config[image_key]) != 1:
            raise IntegrityError(
                f"{filename} does not contain exactly one {config[image_key]} image"
            )


def prepare_tools(root: Path, config: dict[str, str]) -> None:
    require(
        config,
        "DOWNLOAD_TIMEOUT",
        "HELM_BINARY_SHA256",
        "CERT_MANAGER_OCI",
        "CERT_MANAGER_VERSION",
        "CERT_MANAGER_CHART_SHA256",
    )
    timeout = parse_duration(config["DOWNLOAD_TIMEOUT"])
    tools_dir = root / ".tools"
    inputs_dir = tools_dir / "inputs"
    bin_dir = tools_dir / "bin"
    ensure_private_dir(inputs_dir)
    ensure_private_dir(bin_dir)

    downloaded: dict[str, Path] = {}
    for filename, url_key, sha_key in DOWNLOADS:
        downloaded[filename] = _ensure_download(
            inputs_dir,
            filename,
            config[url_key],
            config[sha_key],
            timeout,
        )

    _install_copy(downloaded["kind-linux-amd64"], bin_dir / "kind")
    _install_copy(downloaded["kubectl-linux-amd64"], bin_dir / "kubectl")
    _install_copy(downloaded["clusterctl-linux-amd64"], bin_dir / "clusterctl")
    _install_helm(
        downloaded["helm-linux-amd64.tar.gz"],
        bin_dir / "helm",
        config["HELM_BINARY_SHA256"],
    )
    _ensure_cert_manager_chart(config, inputs_dir, bin_dir, timeout)

    for repository, version_key, commit_key in TAG_SOURCES:
        tag = config[version_key]
        if version_key == "NFS_PROVISIONER_CHART_VERSION":
            tag = f"nfs-subdir-external-provisioner-{tag}"
        _verify_tag(repository, tag, config[commit_key], timeout)

    verify_all_inputs(root, config)
    print(f"prepared verified tools and inputs under {tools_dir}")
