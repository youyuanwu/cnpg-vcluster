from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.controller_catalog import (
    load_management_resources,
    resource_by_kind,
)
from scripts.lib.controller_state import (
    activation_ticket,
    require_clean_controller_state,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads(
    (ROOT / "controller/config/management-resources.json").read_text()
)
BY_RESOURCE = {
    (
        f"{entry['plural']}.{entry['apiVersion'].partition('/')[0]}"
        if "/" in entry["apiVersion"]
        else entry["plural"]
    ): entry
    for entry in CATALOG
}
BY_DISCOVERY = {}
for entry in CATALOG:
    group, separator, version = entry["apiVersion"].partition("/")
    path = f"/apis/{group}/{version}" if separator else f"/api/{group}"
    BY_DISCOVERY.setdefault(path, []).append(entry)
BY_INVENTORY = {}
for entry in CATALOG:
    group, separator, version = entry["apiVersion"].partition("/")
    base = f"/apis/{group}/{version}" if separator else f"/api/{group}"
    namespace = entry["inventoryNamespace"]
    path = (
        f"{base}/namespaces/{namespace}/{entry['plural']}"
        if namespace is not None
        else f"{base}/{entry['plural']}"
    )
    BY_INVENTORY[path] = entry


def response(value="", code=0, error=""):
    if isinstance(value, dict):
        value = json.dumps(value)
    return CompletedProcess([], code, stdout=value, stderr=error)


class Client:
    def __init__(self, handler):
        self.handler = handler

    def kubectl(self, *args, **kwargs):
        result = self.handler(*args, **kwargs)
        if kwargs.get("check", True) and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def json(self, *args):
        return json.loads(self.kubectl(*args, "-o", "json").stdout)


def clean_handler(*args, **_kwargs):
    if "tenants.tenancy.cnpg-vcluster.io" in args:
        return response("")
    raw = next((arg.removeprefix("--raw=") for arg in args if arg.startswith("--raw=")), None)
    if raw is not None:
        if raw in BY_INVENTORY:
            return response(inventory(BY_INVENTORY[raw]["kind"], []))
        return response({
            "resources": [
                {
                    "name": entry["plural"],
                    "kind": entry["kind"],
                    "namespaced": entry["namespaced"],
                }
                for entry in BY_DISCOVERY.get(raw, [])
            ]
        })
    resource = next((arg for arg in args if arg in BY_RESOURCE), None)
    if resource is not None:
        return response(inventory(BY_RESOURCE[resource]["kind"], []))
    if "get" in args:
        return response(code=1, error="NotFound")
    return response()


def item(kind: str, metadata: dict[str, object]) -> dict[str, object]:
    entry = next(entry for entry in CATALOG if entry["kind"] == kind)
    return {
        "apiVersion": entry["apiVersion"],
        "kind": entry["kind"],
        "metadata": {
            "name": "tenant-a",
            "uid": f"{kind.lower()}-uid",
            **({"namespace": "tenant-a"} if entry["namespaced"] else {}),
            **metadata,
        },
    }


def inventory(kind: str, items: list[dict[str, object]]) -> dict[str, object]:
    entry = next(entry for entry in CATALOG if entry["kind"] == kind)
    return {
        "apiVersion": entry["apiVersion"],
        "kind": f"{kind}List",
        "items": items,
    }


def raw_path(args) -> str | None:
    return next(
        (arg.removeprefix("--raw=") for arg in args if arg.startswith("--raw=")),
        None,
    )


def inventory_path(kind: str) -> str:
    return next(path for path, entry in BY_INVENTORY.items() if entry["kind"] == kind)


def prepare_root(directory: str) -> Path:
    root = Path(directory)
    target = root / "controller/config"
    target.mkdir(parents=True)
    target.joinpath("management-resources.json").write_bytes(
        (ROOT / "controller/config/management-resources.json").read_bytes()
    )
    return root


class ControllerStateTests(unittest.TestCase):
    def test_generated_management_catalog_is_complete_and_unique(self) -> None:
        catalog = CATALOG
        identities = {
            (entry["apiVersion"], entry["kind"], entry["plural"])
            for entry in catalog
        }
        self.assertEqual(len(identities), len(catalog))
        for required in (
            ("cluster.x-k8s.io/v1beta2", "Cluster", "clusters"),
            (
                "controlplane.cluster.x-k8s.io/v1alpha2",
                "KamajiControlPlane",
                "kamajicontrolplanes",
            ),
            ("kamaji.clastix.io/v1alpha1", "TenantControlPlane", "tenantcontrolplanes"),
            ("v1", "Namespace", "namespaces"),
            ("v1", "Secret", "secrets"),
            ("coordination.k8s.io/v1", "Lease", "leases"),
        ):
            self.assertIn(required, identities)
        self.assertTrue(
            all(
                set(entry)
                == {
                    "apiVersion",
                    "kind",
                    "plural",
                    "namespaced",
                    "role",
                    "class",
                    "parentKind",
                    "alternateParentKind",
                    "namePolicy",
                    "watched",
                    "watchNameSuffix",
                    "watchClusterLabel",
                    "inventoryPolicy",
                    "inventoryNamespace",
                    "evidencePolicy",
                    "exemptions",
                }
                for entry in catalog
            )
        )
        by_kind = {entry["kind"]: entry for entry in catalog}
        self.assertEqual(
            {
                key: by_kind["Namespace"][key]
                for key in (
                    "namespaced", "role", "namePolicy", "watched",
                    "watchNameSuffix", "watchClusterLabel",
                    "inventoryPolicy", "inventoryNamespace",
                    "evidencePolicy",
                    "exemptions",
                )
            },
            {
                "namespaced": False,
                "role": "namespace",
                "namePolicy": "tenant",
                "watched": True,
                "watchNameSuffix": None,
                "watchClusterLabel": None,
                "inventoryPolicy": "tenant-markers",
                "inventoryNamespace": None,
                "evidencePolicy": "named",
                "exemptions": ["management-infrastructure"],
            },
        )
        resources = load_management_resources(ROOT)
        self.assertEqual(
            resource_by_kind(resources, "Cluster").expected_name("tenant-a"),
            "tenant-a",
        )
        self.assertEqual(
            resource_by_kind(resources, "MachineDeployment").expected_name("tenant-a"),
            "tenant-a-worker",
        )
        self.assertEqual(
            resource_by_kind(resources, "Secret").expected_name("tenant-a"),
            "tenant-a-kubeconfig",
        )
        self.assertIsNone(
            resource_by_kind(resources, "Machine").expected_name("tenant-a")
        )
        self.assertEqual(
            {
                key: by_kind["Secret"][key]
                for key in (
                    "namespaced", "role", "namePolicy", "watched",
                    "watchNameSuffix", "watchClusterLabel",
                    "inventoryPolicy", "inventoryNamespace",
                    "evidencePolicy",
                    "exemptions",
                )
            },
            {
                "namespaced": True,
                "role": "tenant-kubeconfig",
                "namePolicy": "kubeconfig",
                "watched": True,
                "watchNameSuffix": "-kubeconfig",
                "watchClusterLabel": None,
                "inventoryPolicy": "tenant-markers-or-kamaji-owner",
                "inventoryNamespace": None,
                "evidencePolicy": "named",
                "exemptions": ["controller-installation-secrets"],
            },
        )
        self.assertEqual(
            {
                key: by_kind["Lease"][key]
                for key in (
                    "namespaced", "role", "namePolicy", "watched",
                    "watchNameSuffix", "watchClusterLabel",
                    "inventoryPolicy", "inventoryNamespace",
                    "evidencePolicy",
                    "exemptions",
                )
            },
            {
                "namespaced": True,
                "role": "allocation-lease",
                "namePolicy": "allocation",
                "watched": True,
                "watchNameSuffix": None,
                "watchClusterLabel": None,
                "inventoryPolicy": "allocation-markers",
                "inventoryNamespace": "tenant-system",
                "evidencePolicy": "allocation",
                "exemptions": ["controller-leader-election"],
            },
        )

    def test_activation_ticket_is_bound_to_candidate_and_time(self) -> None:
        ticket = activation_ticket("hash-a", "token-a", "hash-old")
        self.assertEqual(
            ticket["data"]["configurationHash"],
            "hash-a",
        )
        self.assertEqual(ticket["data"]["token"], "token-a")
        self.assertEqual(
            ticket["data"]["previousConfigurationHash"],
            "hash-old",
        )
        self.assertEqual(ticket["data"]["hostClean"], "true")
        self.assertTrue(ticket["data"]["createdAt"].endswith("Z"))

    def test_clean_state_accepts_only_empty_authoritative_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ):
            require_clean_controller_state(prepare_root(directory), Client(clean_handler))

    def test_inventory_reads_use_exact_declared_api_paths(self) -> None:
        calls = []

        def handle(*args, **kwargs):
            calls.append(args)
            return clean_handler(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ):
            require_clean_controller_state(prepare_root(directory), Client(handle))
        self.assertTrue(
            any(
                f"--raw={inventory_path('Cluster')}" in call
                for call in calls
            )
        )
        self.assertFalse(
            any("clusters.cluster.x-k8s.io" in call for call in calls)
        )

    def test_offline_registry_container_is_management_infrastructure(self) -> None:
        def docker(*args, **_kwargs):
            command = args[0]
            if command[:3] == ["docker", "ps", "-aq"] and (
                "label=cnpg-vcluster.capi/role" in command
                or "label=cnpg-vcluster.capi/role=offline-registry" in command
            ):
                return response("registry-id")
            return response("")

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            side_effect=docker,
        ):
            require_clean_controller_state(prepare_root(directory), Client(clean_handler))

    def test_offline_registry_with_tenant_role_still_blocks(self) -> None:
        for extra_label in (
            "label=io.x-k8s.kind.role=worker",
            "label=cnpg-vcluster.capi/tenant",
        ):
            def docker(*args, **_kwargs):
                command = args[0]
                if command[:3] == ["docker", "ps", "-aq"] and (
                    "label=cnpg-vcluster.capi/role" in command
                    or "label=cnpg-vcluster.capi/role=offline-registry" in command
                    or extra_label in command
                ):
                    return response("mixed-id")
                return response("")

            with self.subTest(extra_label=extra_label), tempfile.TemporaryDirectory() as directory, patch(
                "scripts.lib.controller_state.run",
                side_effect=docker,
            ), self.assertRaisesRegex(RuntimeError, "CAPD Tenant containers"):
                require_clean_controller_state(prepare_root(directory), Client(clean_handler))

    def test_provider_tenant_lease_and_host_residue_block(self) -> None:
        cases = (
            "tenant",
            "provider",
            "lease",
            "lease-tenant",
            "lease-slot-annotation",
            "lease-tenant-label",
            "volume",
            "container",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = prepare_root(directory)

                def handle(*args, **kwargs):
                    if case == "tenant" and "tenants.tenancy.cnpg-vcluster.io" in args:
                        return response("tenant.tenancy.cnpg-vcluster.io/tenant-a")
                    if case == "provider" and raw_path(args) == inventory_path("Cluster"):
                        return response(inventory("Cluster", [item("Cluster", {})]))
                    if case.startswith("lease") and raw_path(args) == inventory_path("Lease"):
                        metadata = {
                            "lease": {
                                "labels": {
                                    "tenancy.cnpg-vcluster.io/slot-id": "slot-a"
                                }
                            },
                            "lease-tenant": {
                                "annotations": {
                                    "tenancy.cnpg-vcluster.io/tenant-uid": "uid-a"
                                }
                            },
                            "lease-slot-annotation": {
                                "annotations": {
                                    "tenancy.cnpg-vcluster.io/slot-id": "slot-a"
                                }
                            },
                            "lease-tenant-label": {
                                "labels": {
                                    "tenancy.cnpg-vcluster.io/tenant": "tenant-a"
                                }
                            },
                        }[case]
                        metadata["namespace"] = "tenant-system"
                        return response(inventory("Lease", [item("Lease", metadata)]))
                    return clean_handler(*args, **kwargs)

                def docker(*args, **_kwargs):
                    command = args[0]
                    if case == "volume" and command[:4] == [
                        "docker", "volume", "ls", "-q"
                    ] and "--filter" not in command:
                        return response("tenant-a-storage")
                    if case == "container" and command[:3] == [
                        "docker", "ps", "-aq"
                    ] and "label=io.x-k8s.kind.role=worker" in command:
                        return response("container-a")
                    return response("")

                with patch(
                    "scripts.lib.controller_state.run",
                    side_effect=docker,
                ), self.assertRaises(RuntimeError):
                    require_clean_controller_state(root, Client(handle))

    def test_obsolete_local_files_do_not_define_activation_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ):
            root = prepare_root(directory)
            path = root / ".runtime/management/tenant-endpoints.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}")
            require_clean_controller_state(root, Client(clean_handler))

    def test_unmarked_typed_infrastructure_is_exempt(self) -> None:
        def handle(*args, **kwargs):
            if raw_path(args) == inventory_path("Namespace"):
                return response(inventory("Namespace", [item("Namespace", {})]))
            if raw_path(args) == inventory_path("Secret"):
                return response(inventory("Secret", [item("Secret", {})]))
            if raw_path(args) == inventory_path("Lease"):
                return response(inventory(
                    "Lease",
                    [
                        item("Lease", {
                            "name": "tenant-controller.tenancy.cnpg-vcluster.io",
                            "namespace": "tenant-system",
                        })
                    ],
                ))
            return clean_handler(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ):
            require_clean_controller_state(prepare_root(directory), Client(handle))

    def test_tenant_identity_overrides_typed_infrastructure_exemption(self) -> None:
        for kind, metadata in (
            ("Namespace", {
                "annotations": {
                    "tenancy.cnpg-vcluster.io/tenant": "tenant-a"
                }
            }),
            ("Secret", {
                "ownerReferences": [{
                    "apiVersion": "controlplane.cluster.x-k8s.io/v1alpha2",
                    "kind": "KamajiControlPlane",
                    "name": "tenant-a",
                    "uid": "control-plane-uid",
                }]
            }),
            ("Namespace", {
                "labels": {"tenancy.cnpg-vcluster.io/slot-id": "slot-a"}
            }),
            ("Secret", {
                "annotations": {
                    "tenancy.cnpg-vcluster.io/resource": "allocation-lease"
                }
            }),
        ):
            resource = inventory_path(kind)

            def handle(*args, **kwargs):
                if raw_path(args) == resource:
                    return response(inventory(kind, [item(kind, metadata)]))
                return clean_handler(*args, **kwargs)

            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, patch(
                "scripts.lib.controller_state.run",
                return_value=response(""),
            ), self.assertRaisesRegex(RuntimeError, "blocks activation"):
                require_clean_controller_state(prepare_root(directory), Client(handle))

    def test_malformed_owner_and_marker_content_never_mean_exempt(self) -> None:
        for metadata in (
            {"ownerReferences": [None]},
            {"ownerReferences": [{}]},
            {"ownerReferences": [{"apiVersion": "v1", "kind": 3, "name": "x", "uid": "u"}]},
            {"labels": {"tenancy.cnpg-vcluster.io/tenant": {"nested": "bad"}}},
        ):
            def handle(*args, **kwargs):
                if raw_path(args) == inventory_path("Secret"):
                    return response(inventory("Secret", [item("Secret", metadata)]))
                return clean_handler(*args, **kwargs)

            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as directory, patch(
                "scripts.lib.controller_state.run",
                return_value=response(""),
            ), self.assertRaisesRegex(RuntimeError, "invalid inventory identity"):
                require_clean_controller_state(prepare_root(directory), Client(handle))

    def test_inventory_errors_never_mean_absence(self) -> None:
        def handle(*args, **kwargs):
            if raw_path(args) == inventory_path("Cluster"):
                return response(code=1, error="Forbidden")
            return clean_handler(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ), self.assertRaisesRegex(RuntimeError, "failed to inspect"):
            require_clean_controller_state(prepare_root(directory), Client(handle))

    def test_unserved_exact_version_never_means_empty_inventory(self) -> None:
        def handle(*args, **kwargs):
            if "--raw=/apis/cluster.x-k8s.io/v1beta2" in args:
                return response({"resources": []})
            return clean_handler(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ), self.assertRaisesRegex(RuntimeError, "not served"):
            require_clean_controller_state(prepare_root(directory), Client(handle))

    def test_malformed_inventory_never_means_absence(self) -> None:
        def handle(*args, **kwargs):
            if raw_path(args) == inventory_path("Cluster"):
                return response({})
            return clean_handler(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ), self.assertRaisesRegex(RuntimeError, "invalid inventory response"):
            require_clean_controller_state(prepare_root(directory), Client(handle))

    def test_malformed_item_identity_and_scope_never_mean_absence(self) -> None:
        cases = (
            ("Cluster", {"annotations": []}),
            ("Lease", {"namespace": "wrong-system"}),
        )
        for kind, metadata in cases:
            resource = inventory_path(kind)

            def handle(*args, **kwargs):
                if raw_path(args) == resource:
                    return response(inventory(kind, [item(kind, metadata)]))
                return clean_handler(*args, **kwargs)

            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, patch(
                "scripts.lib.controller_state.run",
                return_value=response(""),
            ), self.assertRaisesRegex(RuntimeError, "invalid inventory identity"):
                require_clean_controller_state(prepare_root(directory), Client(handle))

    def test_unknown_policy_is_rejected_before_empty_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ):
            root = prepare_root(directory)
            path = root / "controller/config/management-resources.json"
            catalog = json.loads(path.read_text())
            catalog[0]["inventoryPolicy"] = "unknown"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "metadata is invalid"):
                require_clean_controller_state(root, Client(clean_handler))

    def test_unknown_exemption_is_rejected_before_empty_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.lib.controller_state.run",
            return_value=response(""),
        ):
            root = prepare_root(directory)
            path = root / "controller/config/management-resources.json"
            catalog = json.loads(path.read_text())
            next(entry for entry in catalog if entry["kind"] == "Secret")[
                "exemptions"
            ] = ["unknown-exemption"]
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "exemptions are invalid"):
                require_clean_controller_state(root, Client(clean_handler))

    def test_catalog_fixture_changes_named_resource_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = prepare_root(directory)
            path = root / "controller/config/management-resources.json"
            catalog = json.loads(path.read_text())
            deployment = next(
                entry for entry in catalog
                if entry["kind"] == "MachineDeployment"
            )
            deployment["namePolicy"] = "tenant"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            resources = load_management_resources(root)
            self.assertEqual(
                resource_by_kind(resources, "MachineDeployment").expected_name(
                    "tenant-a"
                ),
                "tenant-a",
            )


if __name__ == "__main__":
    unittest.main()
