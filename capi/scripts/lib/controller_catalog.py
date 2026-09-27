"""Generated local-operator resource identities.

Allocation names, fixed controller infrastructure, provider-only CRDs,
break-glass allowlists, test fixtures, and tenant-internal resources remain
owned by their domain modules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


FIELDS = {
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
    "watchByCluster",
    "inventoryPolicy",
    "inventoryNamespace",
    "exemptions",
}
CLASSES = {"root", "descendant", "typed"}
NAME_POLICIES = {"tenant", "worker", "kubeconfig", "observed", "allocation"}
INVENTORY_POLICIES = {
    "block-any-instance",
    "tenant-markers",
    "tenant-markers-or-kamaji-owner",
    "allocation-markers",
}


@dataclass(frozen=True)
class ManagementResource:
    api_version: str
    kind: str
    plural: str
    namespaced: bool
    role: str
    resource_class: str
    parent_kind: str | None
    alternate_parent_kind: str | None
    name_policy: str
    watched: bool
    watch_by_cluster: bool
    inventory_policy: str
    inventory_namespace: str | None
    exemptions: tuple[str, ...]

    @property
    def group(self) -> str:
        return self.api_version.partition("/")[0] if "/" in self.api_version else ""

    @property
    def version(self) -> str:
        return self.api_version.rpartition("/")[2]

    @property
    def kubectl_resource(self) -> str:
        return f"{self.plural}.{self.group}" if self.group else self.plural

    @property
    def discovery_path(self) -> str:
        if self.group:
            return f"/apis/{self.group}/{self.version}"
        return f"/api/{self.version}"

    @property
    def inventory_path(self) -> str:
        base = self.discovery_path
        if self.inventory_namespace is not None:
            return f"{base}/namespaces/{self.inventory_namespace}/{self.plural}"
        return f"{base}/{self.plural}"

    def expected_name(self, tenant: str) -> str | None:
        if self.name_policy == "tenant":
            return tenant
        if self.name_policy == "worker":
            return f"{tenant}-worker"
        if self.name_policy == "kubeconfig":
            return f"{tenant}-kubeconfig"
        return None


def load_management_resources(root: Path) -> tuple[ManagementResource, ...]:
    path = root / "controller" / "config" / "management-resources.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("management resource catalog is invalid") from exc
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("management resource catalog is invalid")
    resources = tuple(_parse_resource(entry) for entry in payload)
    identities = {
        (resource.api_version, resource.kind, resource.plural)
        for resource in resources
    }
    if len(identities) != len(resources):
        raise RuntimeError("management resource catalog identity is duplicated")
    kinds = {resource.kind for resource in resources}
    if any(
        parent is not None and parent not in kinds
        for resource in resources
        for parent in (resource.parent_kind, resource.alternate_parent_kind)
    ):
        raise RuntimeError("management resource catalog parent is unknown")
    return resources


def _parse_resource(entry: object) -> ManagementResource:
    if not isinstance(entry, dict) or set(entry) != FIELDS:
        raise RuntimeError("management resource catalog is invalid")
    strings = ("apiVersion", "kind", "plural", "role")
    if any(not isinstance(entry[field], str) or not entry[field] for field in strings):
        raise RuntimeError("management resource catalog metadata is invalid")
    optional_strings = ("parentKind", "alternateParentKind", "inventoryNamespace")
    if any(
        entry[field] is not None
        and (not isinstance(entry[field], str) or not entry[field])
        for field in optional_strings
    ):
        raise RuntimeError("management resource catalog metadata is invalid")
    exemptions = entry["exemptions"]
    if (
        not isinstance(entry["namespaced"], bool)
        or not isinstance(entry["watched"], bool)
        or not isinstance(entry["watchByCluster"], bool)
        or entry["class"] not in CLASSES
        or entry["namePolicy"] not in NAME_POLICIES
        or entry["inventoryPolicy"] not in INVENTORY_POLICIES
        or not isinstance(exemptions, list)
        or not all(isinstance(value, str) and value for value in exemptions)
    ):
        raise RuntimeError("management resource catalog metadata is invalid")
    if (
        entry["inventoryNamespace"] is not None
        and not entry["namespaced"]
    ):
        raise RuntimeError("cluster-scoped catalog resource has an inventory namespace")
    if (
        entry["inventoryPolicy"] == "block-any-instance"
        and exemptions
    ) or (
        entry["inventoryPolicy"] != "block-any-instance"
        and not exemptions
    ):
        raise RuntimeError("management resource catalog exemptions are invalid")
    return ManagementResource(
        api_version=entry["apiVersion"],
        kind=entry["kind"],
        plural=entry["plural"],
        namespaced=entry["namespaced"],
        role=entry["role"],
        resource_class=entry["class"],
        parent_kind=entry["parentKind"],
        alternate_parent_kind=entry["alternateParentKind"],
        name_policy=entry["namePolicy"],
        watched=entry["watched"],
        watch_by_cluster=entry["watchByCluster"],
        inventory_policy=entry["inventoryPolicy"],
        inventory_namespace=entry["inventoryNamespace"],
        exemptions=tuple(exemptions),
    )


def resource_by_kind(
    resources: tuple[ManagementResource, ...],
    kind: str,
) -> ManagementResource:
    matches = [resource for resource in resources if resource.kind == kind]
    if len(matches) != 1:
        raise RuntimeError(f"management resource kind is not unique: {kind}")
    return matches[0]
