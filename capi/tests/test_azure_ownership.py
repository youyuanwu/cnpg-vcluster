from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.lib.azure.ownership import (
    classify_azure_owned_resources,
    expected_azure_tags,
    normalize_resource_id,
    observe_azure_owned_resources,
    tenant_tagged_azure_resources,
)
from tests.azure_fixtures import AzureFixtureMixin


VMSS = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/"
    "resourceGroups/yy-cv-rg/providers/Microsoft.Compute/"
    "virtualMachineScaleSets/tenant-c-worker"
)


def tenant() -> dict[str, object]:
    return {
        "metadata": {"name": "tenant-c", "uid": "tenant-uid"},
        "status": {
            "provider": {
                "type": "azure",
                "binding": {
                    "specificationSha256": "spec-sha",
                    "foundationSha256": "foundation-sha",
                    "operationId": "operation-1",
                    "resourceGroupId": "/subscriptions/x/resourceGroups/yy-cv-rg",
                    "virtualNetworkId": "/subscriptions/x/resourceGroups/yy-cv-rg/providers/Microsoft.Network/virtualNetworks/yy-cv-vnet",
                    "tenantSubnetId": "/subscriptions/x/resourceGroups/yy-cv-rg/providers/Microsoft.Network/virtualNetworks/yy-cv-vnet/subnets/tenant",
                    "identityId": "/subscriptions/x/resourceGroups/yy-cv-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/yy-cv",
                },
                "vmss": {
                    "id": VMSS,
                    "instanceIds": [
                        f"{VMSS}/virtualMachines/{value}" for value in range(3)
                    ],
                },
                "providerResources": [
                    {
                        "apiVersion": "network.azure.com/v1api20220701",
                        "kind": "PublicIPAddress",
                        "namespace": "tenant-c",
                        "name": "tenant-c-public-ip",
                        "uid": "public-ip-uid",
                        "resourceId": "/subscriptions/x/resourceGroups/yy-cv-rg/providers/Microsoft.Network/publicIPAddresses/tenant-c",
                    }
                ],
            }
        },
    }


class AzureOwnershipTests(AzureFixtureMixin, unittest.TestCase):
    def test_provider_id_and_arm_id_share_one_canonical_identity(self) -> None:
        resource_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachineScaleSets/pool/virtualMachines/1"
        self.assertEqual(
            normalize_resource_id("azure://" + resource_id),
            resource_id.lower(),
        )
        self.assertEqual(normalize_resource_id(resource_id + "/"), resource_id.lower())

    def test_expected_tags_are_derived_from_operator_binding(self) -> None:
        self.assertEqual(
            expected_azure_tags(tenant()),
            {
                "cnpg-vcluster-tenant": "tenant-c",
                "cnpg-vcluster-profile": "azure",
                "cnpg-vcluster-spec-sha256": "spec-sha",
                "cnpg-vcluster-foundation-sha256": "foundation-sha",
                "cnpg-vcluster-operation-id": "operation-1",
            },
        )

    def test_classifier_requires_exact_tags_and_known_types(self) -> None:
        tags = expected_azure_tags(tenant())
        public_ip = tenant()["status"]["provider"]["providerResources"][0][
            "resourceId"
        ]
        resources = [
            {
                "id": public_ip,
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": tags,
            }
        ]
        self.assertEqual(
            classify_azure_owned_resources(resources, tags),
            [
                {
                    "id": public_ip,
                    "type": "microsoft.network/publicipaddresses",
                }
            ],
        )
        resources[0]["tags"] = {**tags, "cnpg-vcluster-operation-id": "foreign"}
        with self.assertRaisesRegex(RuntimeError, "foreign Azure tags"):
            classify_azure_owned_resources(resources, tags)
        resources[0] = {
            "id": public_ip,
            "type": "Microsoft.Network/privateEndpoints",
            "tags": tags,
        }
        with self.assertRaisesRegex(RuntimeError, "unknown Azure tenant"):
            classify_azure_owned_resources(resources, tags)

    def test_observation_binds_status_vmss_instances_nics_and_resources(self) -> None:
        root = self.make_root()
        payload = tenant()
        tags = expected_azure_tags(payload)
        public_ip = payload["status"]["provider"]["providerResources"][0][
            "resourceId"
        ]
        resource_list = [
            {
                "id": VMSS,
                "type": "Microsoft.Compute/virtualMachineScaleSets",
                "tags": tags,
            },
            {
                "id": public_ip,
                "type": "Microsoft.Network/publicIPAddresses",
                "tags": tags,
            },
        ]
        instances = [
            {
                "id": f"{VMSS}/virtualMachines/{value}",
                "type": None,
            }
            for value in range(3)
        ]
        nics = [
            {
                "id": f"{VMSS}/virtualMachines/{value}/networkInterfaces/nic-{value}",
                "type": None,
                "virtualMachineId": f"{VMSS}/virtualMachines/{value}",
            }
            for value in range(3)
        ]
        with (
            patch(
                "scripts.lib.azure.ownership._json",
                side_effect=(resource_list, instances),
            ),
            patch(
                "scripts.lib.azure.ownership._az",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(nics), stderr=""
                ),
            ),
        ):
            observed = observe_azure_owned_resources(
                root, {"AZURE_PREFIX": "yy-cv"}, payload
            )
        self.assertEqual(8, len(observed["azure"]))
        self.assertEqual("PublicIPAddress", observed["provider"][0]["kind"])

    def test_tagged_absence_observation_is_external_and_exact(self) -> None:
        with patch(
            "scripts.lib.azure.ownership._json",
            return_value=[
                {
                    "id": "/tenant",
                    "type": "Microsoft.Network/publicIPAddresses",
                    "tags": {"cnpg-vcluster-tenant": "tenant-c"},
                },
                {
                    "id": "/other",
                    "type": "Microsoft.Network/publicIPAddresses",
                    "tags": {"cnpg-vcluster-tenant": "other"},
                },
            ],
        ):
            self.assertEqual(
                tenant_tagged_azure_resources(
                    {"AZURE_PREFIX": "yy-cv"}, "tenant-c"
                ),
                [
                    {
                        "id": "/tenant",
                        "type": "microsoft.network/publicipaddresses",
                    }
                ],
            )
