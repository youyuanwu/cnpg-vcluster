from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.lib.azure.operator import (
    create_tenant,
    delete_tenant,
    tenant_document,
    tenant_status,
)
from scripts.lib.azure.proof import (
    AzureDeletionProof,
    _specification_sha256,
    capture_operator_deletion_proof,
    prove_operator_deletion,
)
from tests.azure_fixtures import AzureFixtureMixin, CONTROLLER_IMAGE, FOUNDATION


class AzureOperatorTests(AzureFixtureMixin, unittest.TestCase):
    def test_document_maps_existing_json_spec_to_azure_tenant(self) -> None:
        spec = self.spec(workers=3)
        self.assertEqual(
            {
                "apiVersion": "tenancy.cnpg-vcluster.io/v1alpha2",
                "kind": "Tenant",
                "metadata": {"name": "tenant-c"},
                "spec": {
                    "kubernetesVersion": "1.32.13",
                    "workers": 3,
                    "provider": {
                        "type": "azure",
                        "podCIDR": "10.72.0.0/16",
                        "serviceCIDR": "10.142.0.0/16",
                    },
                },
            },
            tenant_document(spec),
        )

    def test_status_requires_current_generation_conditions(self) -> None:
        payload = {
            "metadata": {"name": "tenant-c", "generation": 2},
            "status": {
                "observedGeneration": 1,
                "phase": "Ready",
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "observedGeneration": 1,
                        "message": "stale",
                    }
                ],
            },
        }
        self.assertEqual(
            "progressing",
            tenant_status("tenant-c", payload).classification,
        )
        payload["status"]["observedGeneration"] = 2
        payload["status"]["conditions"][0]["observedGeneration"] = 2
        self.assertEqual("ready", tenant_status("tenant-c", payload).classification)

    def test_create_applies_only_tenant_and_waits_for_ready(self) -> None:
        root = self.make_root()
        spec = self.spec()
        calls = []
        ready = {
            "metadata": {"name": spec.name, "generation": 1},
            "status": {
                "observedGeneration": 1,
                "phase": "Ready",
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "observedGeneration": 1,
                    },
                    {
                        "type": "FoundationReady",
                        "status": "True",
                        "observedGeneration": 1,
                    },
                ],
            },
        }

        def kubectl(_root, *arguments, **kwargs):
            calls.append((arguments, kwargs))
            stdout = json.dumps(ready) if arguments[0] == "get" else ""
            return CompletedProcess([], 0, stdout=stdout, stderr="")

        with patch("scripts.lib.azure.operator._kubectl", side_effect=kubectl):
            create_tenant(root, spec)
        self.assertEqual("apply", calls[0][0][0])
        document = json.loads(calls[0][1]["input_text"])
        self.assertEqual("Tenant", document["kind"])
        self.assertEqual("azure", document["spec"]["provider"]["type"])

    def test_delete_captures_proof_then_deletes_waits_and_proves(self) -> None:
        root = self.make_root()
        payload = {"metadata": {"name": "tenant-c"}}
        events = []
        with (
            patch(
                "scripts.lib.azure.operator.read_tenant",
                side_effect=[payload, None],
            ),
            patch(
                "scripts.lib.azure.operator.capture_operator_deletion_proof",
                return_value=AzureDeletionProof(
                    "tenant-c", {}, FOUNDATION, "vmss", ("vmss",)
                ),
            ) as capture,
            patch(
                "scripts.lib.azure.operator._kubectl",
                return_value=CompletedProcess([], 0, stdout="", stderr=""),
            ) as kubectl,
            patch(
                "scripts.lib.azure.operator.prove_operator_deletion",
                side_effect=lambda *_args: events.append("proved"),
            ) as prove,
        ):
            delete_tenant(root, "tenant-c")
        capture.assert_called_once()
        self.assertEqual("delete", kubectl.call_args.args[1])
        self.assertIn("--wait=false", kubectl.call_args.args)
        prove.assert_called_once()
        self.assertEqual(["proved"], events)


class AzureProofTests(AzureFixtureMixin, unittest.TestCase):
    def test_specification_hash_matches_rust_canonical_vector(self) -> None:
        self.assertEqual(
            _specification_sha256(
                {
                    "kubernetesVersion": "v1.36.4",
                    "workers": 3,
                    "provider": {
                        "type": "azure",
                        "podCIDR": "10.244.0.0/16",
                        "serviceCIDR": "10.96.0.0/16",
                    },
                }
            ),
            "61bf78756f6c9cc847f31de706048bae68b07b1cf8c0cd856142931854ac1885",
        )

    def test_capture_binds_operator_foundation_and_resource_ids(self) -> None:
        root = self.make_root()
        config = {
            "AZURE_CONTROLLER_REPOSITORY": "tenant-controller",
        }
        foundation = dict(FOUNDATION)
        foundation["controllerImage"] = CONTROLLER_IMAGE
        foundation["azureProviderConfigUid"] = "azure-provider-config-uid"
        specification = {
            "kubernetesVersion": "1.32.13",
            "workers": 1,
            "provider": {
                "type": "azure",
                "podCIDR": "10.72.0.0/16",
                "serviceCIDR": "10.142.0.0/16",
            },
        }
        specification_sha256 = _specification_sha256(specification)
        operation_id = "tenant-" + hashlib.sha256(
            b"azure-tenant-operation-v1\0"
            + b"tenant-uid\0"
            + specification_sha256.encode()
        ).hexdigest()
        provider_config = {
            "foundationSha256": "foundation-provider-sha",
        }
        provider_config_sha256 = hashlib.sha256(
            json.dumps(
                provider_config,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        tenant = {
            "metadata": {
                "name": "tenant-c",
                "uid": "tenant-uid",
            },
            "spec": specification,
            "status": {
                "provider": {
                    "binding": {
                        "tenantUID": "tenant-uid",
                        "specificationSha256": specification_sha256,
                        "operationId": operation_id,
                        "foundationSha256": "foundation-provider-sha",
                        "foundationDefaultsSha256": foundation[
                            "foundationDefaultsSha256"
                        ],
                        "controllerImage": foundation["controllerImage"],
                        "providerConfigUID": foundation["azureProviderConfigUid"],
                        "providerConfigSha256": provider_config_sha256,
                        "resourceGroupId": foundation["resourceGroupId"],
                        "virtualNetworkId": foundation["vnetId"],
                        "tenantSubnetId": foundation["tenantSubnetId"],
                        "identityId": foundation["identityId"],
                    },
                    "vmss": {"id": "/tenant/vmss"},
                    "providerResources": [
                        {"resourceId": "/tenant/public-ip"},
                        {"resourceId": foundation["vnetId"]},
                    ],
                }
            },
        }
        with (
            patch(
                "scripts.lib.azure.proof._inspect_foundation",
                return_value=(foundation, True, ()),
            ),
            patch("scripts.lib.azure.proof.load_inventory", return_value={}),
            patch(
                "scripts.lib.azure.proof._azure_provider_configuration",
                return_value=provider_config,
            ),
        ):
            proof = capture_operator_deletion_proof(root, config, tenant)
        self.assertEqual(
            ("/tenant/public-ip", "/tenant/vmss"),
            proof.resource_ids,
        )

    def test_external_proof_requires_tagged_and_recorded_absence(self) -> None:
        root = Path(tempfile.mkdtemp())
        proof = AzureDeletionProof(
            "tenant-c",
            {},
            FOUNDATION,
            None,
            ("/tenant/public-ip",),
        )
        with (
            patch(
                "scripts.lib.azure.proof._inspect_foundation",
                return_value=(dict(FOUNDATION), True, ()),
            ),
            patch(
                "scripts.lib.azure.proof._tenant_tagged_azure_resources",
                return_value=[],
            ),
            patch("scripts.lib.azure.proof.names", return_value={"resourceGroup": "rg"}),
            patch("scripts.lib.azure.proof._json", return_value=[]),
        ):
            prove_operator_deletion(root, {}, proof)
