from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.lib.controller_foundation import canonical_hash, foundation_payload, resolve_slots
from scripts.lib.images import WORKER_IMAGE_KEYS


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "controller" / "tests" / "fixtures" / "foundation-schema3.json"
CATALOG = ROOT / "config" / "tenant-allocation-slots.json"


class FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "SPIKE_API_VIP_SLOT": "2",
            "MANAGEMENT_POD_CIDR": "10.210.0.0/16",
            "MANAGEMENT_SERVICE_CIDR": "10.211.0.0/16",
            "SPIKE_POD_CIDR": "10.72.0.0/16",
            "SPIKE_SERVICE_CIDR": "10.142.0.0/16",
            "KUBERNETES_VERSION": "1.36.4",
            "OWNERSHIP_LABEL": "example.io/owned",
            "LAB_PREFIX": "example",
            "SPIKE_API_PORT": "6443",
            "SPIKE_CLUSTER_DOMAIN": "spike.capi.local",
            "KIND_NODE_IMAGE": "kind:one",
            "SPIKE_STORAGE_CONTAINER_PATH": "/var/lib/storage",
            "KONNECTIVITY_SERVER_IMAGE": "server:one",
            "KONNECTIVITY_AGENT_IMAGE": "agent:one",
        }
        for key in WORKER_IMAGE_KEYS:
            self.config[key] = f"{key.lower()}@sha256:one"
            self.config[f"{key}_TAGGED"] = f"docker.io/example/{key.lower()}:one"
        self.network = {
            "network_id": "network-one", "subnet": "172.18.0.0/16",
            "pool_start": "172.18.255.223", "pool_end": "172.18.255.238",
            "slots": {"spike": "172.18.255.225"},
        }

    def test_golden_hash_matches_rust_fixture(self) -> None:
        fixture = json.loads(FIXTURE.read_text())
        raw = fixture["foundation"]
        self.assertEqual(canonical_hash(raw), fixture["sha256"])
        self.assertEqual(canonical_hash(json.dumps(raw, indent=2)), fixture["sha256"])
        mutable = copy.deepcopy(raw)
        mutable["controllerImage"] = "different"
        mutable["mutationEnabled"] = False
        self.assertEqual(canonical_hash(mutable), fixture["sha256"])
        for change in (
            lambda data: data["slots"][0].update(endpoint="172.18.255.224"),
            lambda data: data["cache"]["imageArchives"][0].update(path="different.tar"),
            lambda data: data.update(extraMetadata={"version": 2}),
        ):
            changed = copy.deepcopy(raw)
            change(changed)
            self.assertNotEqual(canonical_hash(changed), fixture["sha256"])
        with self.assertRaises(ValueError):
            canonical_hash("[]")

    def test_complete_deterministic_catalog_and_existing_networks(self) -> None:
        slots = resolve_slots(ROOT, self.config, self.network)
        self.assertEqual(len(slots), 15)
        self.assertEqual([entry["vipOrdinal"] for entry in slots],
                         [0, 1, *range(3, 16)])
        self.assertEqual([entry["endpoint"] for entry in slots[:3]],
                         ["172.18.255.223", "172.18.255.224", "172.18.255.226"])
        self.assertEqual(
            [(slot["podCIDR"], slot["serviceCIDR"]) for slot in slots[:3]],
            [("10.73.0.0/16", "10.143.0.0/16"),
             ("10.74.0.0/16", "10.144.0.0/16"),
             ("10.75.0.0/16", "10.145.0.0/16")])
        self.assertEqual(slots, resolve_slots(ROOT, self.config, self.network))

    def test_rejects_network_capacity_and_reservation_conflicts(self) -> None:
        for change in (
            lambda cfg, net: net.update(pool_end="172.18.255.237"),
            lambda cfg, net: net.update(pool_end="172.18.255.239"),
            lambda cfg, net: net.update(pool_start="172.19.0.1"),
            lambda cfg, net: net.update(pool_start="172.18.255.239"),
            lambda cfg, net: net["slots"].update(spike="172.18.255.226"),
            lambda cfg, net: cfg.update(SPIKE_API_VIP_SLOT="16"),
            lambda cfg, net: cfg.update(SPIKE_API_VIP_SLOT="-1"),
            lambda cfg, net: cfg.update(SPIKE_API_VIP_SLOT="0"),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR="172.18.0.0/16"),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR="10.73.0.0/16"),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR="10.72.0.1/16"),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR="2001:db8::/32"),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR=cfg["MANAGEMENT_POD_CIDR"]),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR="10.210.0.0/17"),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR="garbage"),
            lambda cfg, net: cfg.update(SPIKE_POD_CIDR="172.18.255.224/32"),
        ):
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                config = copy.deepcopy(self.config)
                network = copy.deepcopy(self.network)
                change(config, network)
                resolve_slots(ROOT, config, network)

    def test_rejects_malformed_duplicate_and_missing_catalog_entries(self) -> None:
        catalog = json.loads(CATALOG.read_text())
        for change in (
            lambda items: items[1].update(slotId=items[0]["slotId"]),
            lambda items: items[1].update(vipOrdinal=items[0]["vipOrdinal"]),
            lambda items: items[1].update(vipOrdinal=2),
            lambda items: items[1].update(vipOrdinal=16),
            lambda items: items[1].update(vipOrdinal=-1),
            lambda items: items[1].update(vipOrdinal="1"),
            lambda items: items[1].update(slotId="BAD/ID"),
            lambda items: items[1].update(podCIDR=items[0]["podCIDR"]),
            lambda items: items[1].update(podCIDR="10.73.0.0/17"),
            lambda items: items[1].update(serviceCIDR="10.73.0.0/16"),
            lambda items: items[1].update(podCIDR="10.74.0.1/16"),
            lambda items: items.pop(),
            lambda items: items[0].update(unexpected="metadata"),
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "config").mkdir()
                changed = copy.deepcopy(catalog)
                change(changed)
                (root / "config" / CATALOG.name).write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    resolve_slots(root, self.config, self.network)

    def test_producer_schema_and_mutable_hash(self) -> None:
        cache = SimpleNamespace(
            generation=Path("generation-one"),
            inventory={"imageArchives": [
                {"key": key, "path": f"images/{key}.tar", "sha256": "a" * 64}
                for key in WORKER_IMAGE_KEYS]},
        )
        with patch("scripts.lib.management.require_management_ownership"):
            published = foundation_payload(
                ROOT, self.config, self.network, "controller:one", cache, None)
            changed = foundation_payload(
                ROOT, self.config, self.network, "controller:two", cache, None,
                mutation_enabled=False)
        data = json.loads(published["data"]["foundation.json"])
        self.assertEqual(data["schema"], 3)
        self.assertEqual(len(data["slots"]), 15)
        self.assertEqual(set(data), {"schema", "networkId", "subnet", "reservedCIDRs",
                                    "allowedSubnets", "kubernetesVersion", "controllerImage",
                                    "mutationEnabled", "offlineEnforced", "slots",
                                    "cache", "registry", "inputs"})
        self.assertEqual(set(data["cache"]), {"generation", "imageArchives"})
        self.assertEqual(published["data"]["foundation.sha256"],
                         canonical_hash(published["data"]["foundation.json"]))
        self.assertEqual(published["data"]["foundation.sha256"],
                         changed["data"]["foundation.sha256"])

    def test_producer_rejects_invalid_archives_and_offline_registry(self) -> None:
        archives = [
            {"key": key, "path": f"images/{key}.tar", "sha256": "a" * 64}
            for key in WORKER_IMAGE_KEYS
        ]
        cache = SimpleNamespace(
            generation=Path("generation-one"), inventory={"imageArchives": archives})
        with patch("scripts.lib.management.require_management_ownership"):
            for invalid in (
                [*archives, archives[0]],
                [{**archives[0], "path": "../escape.tar"}, *archives[1:]],
                [{**archives[0], "sha256": "invalid"}, *archives[1:]],
                archives[1:],
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    cache.inventory["imageArchives"] = invalid
                    foundation_payload(
                        ROOT, self.config, self.network, "controller:one", cache, None)
            cache.inventory["imageArchives"] = archives
            with patch.dict("os.environ", {"CAPI_OFFLINE_ENFORCED": "1"}):
                with self.assertRaises(ValueError):
                    foundation_payload(
                        ROOT, self.config, self.network, "controller:one", cache, None)
                with self.assertRaises(ValueError):
                    foundation_payload(
                        ROOT, self.config, self.network, "controller:one", cache,
                        {"address": "172.19.0.10"})
                published = foundation_payload(
                    ROOT, self.config, self.network, "controller:one", cache,
                    {"address": "172.18.0.10", "generation": "one", "identifier": "id"})
            data = json.loads(published["data"]["foundation.json"])
            self.assertTrue(data["offlineEnforced"])
            self.assertEqual(data["registry"], {"address": "172.18.0.10", "port": 5000})


if __name__ == "__main__":
    unittest.main()
