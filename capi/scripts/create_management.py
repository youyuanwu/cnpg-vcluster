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


def create_management(root: Path, config: dict[str, str]) -> None:
    run_preflight(root, config)
    client = reconcile_kind(root, config)
    network = reconcile_network(root, config)
    reconcile_cert_manager(root, config, client)
    reconcile_metallb(root, config, client, network)
    reconcile_kamaji(root, config, client)
    reconcile_providers(root, config, client)
    print("management cluster and lifecycle controllers are ready")
