from __future__ import annotations

from pathlib import Path

from scripts.lib.management import (
    reconcile_cert_manager,
    reconcile_kamaji,
    reconcile_kind,
    reconcile_metallb,
    reconcile_network,
)
from scripts.lib.providers import reconcile_providers
from scripts.preflight import run_preflight
from scripts.lib.images import (
    MANAGEMENT_IMAGE_KEYS,
    import_container_images,
    restore_host_images,
    enforce_offline_node_egress,
)


def create_management(root: Path, config: dict[str, str]) -> None:
    run_preflight(root, config)
    restore_host_images(
        root,
        config,
        ("KIND_NODE_IMAGE", *MANAGEMENT_IMAGE_KEYS),
    )
    client = reconcile_kind(root, config)
    import_container_images(
        root,
        config,
        f"{config['KIND_CLUSTER_NAME']}-control-plane",
        MANAGEMENT_IMAGE_KEYS,
    )
    network = reconcile_network(root, config)
    enforce_offline_node_egress(
        root,
        config,
        f"{config['KIND_CLUSTER_NAME']}-control-plane",
    )
    reconcile_cert_manager(root, config, client)
    reconcile_metallb(root, config, client, network)
    reconcile_kamaji(root, config, client)
    reconcile_providers(root, config, client)
    print("management cluster and lifecycle controllers are ready")
