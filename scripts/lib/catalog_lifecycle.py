from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from subprocess import CompletedProcess


UUID = re.compile(r"^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$")
SCHEMA_VERSION = 6
Get = Callable[[str], str]
Mutate = Callable[[str, str, dict[str, object]], CompletedProcess[str]]


def _text(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"catalog {description} is missing")
    return value


def _uid(value: object, description: str) -> str:
    result = _text(value, description)
    if not UUID.fullmatch(result):
        raise RuntimeError(f"catalog {description} is invalid")
    return result


def envelope(raw: str, description: str) -> dict[str, object]:
    try:
        response = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"{description} is not JSON") from exc
    if (
        not isinstance(response, dict)
        or set(response) != {"schemaVersion", "data"}
        or response["schemaVersion"] != SCHEMA_VERSION
        or not isinstance(response["data"], dict)
    ):
        raise RuntimeError(f"{description} is not a schema-v6 envelope")
    return response["data"]


def require_stale_identity(response: CompletedProcess[str]) -> None:
    try:
        body = json.loads(response.stdout)
    except ValueError as exc:
        raise RuntimeError("stale identity rejection was not a schema-v6 error") from exc
    if (
        response.returncode == 0 or not response.stderr.startswith("HTTP 409:")
        or not isinstance(body, dict)
        or set(body) != {"schemaVersion", "error"}
        or body["schemaVersion"] != SCHEMA_VERSION
        or not isinstance(body["error"], dict)
        or body["error"].get("code") != "stale-identity"
        or body["error"].get("retryable") is not False
    ):
        raise RuntimeError("stale identity was not rejected as a conflict")


def validate_catalog(
    value: Mapping[str, object], tenant: str, tenant_uid: str,
    *, expected_uid: str | None = None,
) -> dict[str, object]:
    if set(value) != {
        "tenant", "tenantUid", "catalogUid", "resourceVersion", "closed",
        "capabilityAvailable", "databases",
    } or value["tenant"] != tenant or value["tenantUid"] != tenant_uid:
        raise RuntimeError("catalog Tenant identity or response shape changed")
    catalog_uid = _uid(value["catalogUid"], "UID")
    if expected_uid is not None and catalog_uid != expected_uid:
        raise RuntimeError("catalog UID changed")
    _text(value["resourceVersion"], "resourceVersion")
    if type(value["closed"]) is not bool or type(value["capabilityAvailable"]) is not bool:
        raise RuntimeError("catalog capability or closure is invalid")
    entries = value["databases"]
    if not isinstance(entries, list) or len(entries) > 3:
        raise RuntimeError("catalog entries are invalid")
    seen_names: set[str] = set()
    seen_uids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "logicalUid", "name", "instances", "deleting", "phase",
            "observedGeneration", "provider", "namespace", "namespaceUid",
            "cluster", "clusterUid", "credentialUid", "queryIdentity",
            "readyInstances", "storageRequestedBytes", "storageHealthy",
            "storage", "conditions", "finalization", "instanceTopology",
            "blockers", "topology",
        }:
            raise RuntimeError("catalog entry shape is invalid")
        logical_uid = _uid(entry["logicalUid"], "entry UID")
        name = _text(entry["name"], "entry name")
        if (
            logical_uid in seen_uids or name in seen_names
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?", name)
        ):
            raise RuntimeError("catalog entries are not unique")
        seen_uids.add(logical_uid)
        seen_names.add(name)
        if (
            type(entry["instances"]) is not int or not 1 <= entry["instances"] <= 3
            or type(entry["deleting"]) is not bool
            or entry["phase"] not in {
                "progressing", "ready", "deleting", "degraded", "ownership-invalid",
            }
            or not isinstance(entry["storage"], list)
            or not isinstance(entry["instanceTopology"], list)
            or not isinstance(entry["conditions"], list)
            or not isinstance(entry["blockers"], list)
            or not isinstance(entry["topology"], dict)
        ):
            raise RuntimeError("catalog entry state is invalid")
    return dict(value)


def ready_entries(catalog: Mapping[str, object], names: set[str], provider: str) -> dict[str, dict]:
    entries = catalog["databases"]
    if (
        not isinstance(entries, list)
        or {entry["name"] for entry in entries} != names
        or catalog["closed"] is not False
        or catalog["capabilityAvailable"] is not True
    ):
        raise RuntimeError("catalog is not the requested open three-entry set")
    result = {}
    for entry in entries:
        count = entry["instances"]
        topology = entry["topology"]
        instances = entry["instanceTopology"]
        storage = entry["storage"]
        identities = entry["queryIdentity"]
        nodes = topology.get("nodes")
        if (
            count != 3 or entry["phase"] != "ready"
            or entry["deleting"] or entry["provider"] != provider
            or entry["readyInstances"] != 3 or entry["storageHealthy"] != 3
            or len(instances) != 3 or len(storage) != 3
            or not all(item.get("ready") is True and _text(item.get("uid"), "instance UID")
                       for item in instances)
            or len({item["uid"] for item in instances}) != 3
            or {item.get("ordinal") for item in storage} != {1, 2, 3}
            or not all(item.get("healthy") is True and item.get("pvUid")
                       and item.get("pvcUid") and
                       (provider != "azure" or item.get("diskUid")) for item in storage)
            or not isinstance(identities, dict)
            or identities.get("clusterUid") != entry["clusterUid"]
            or identities.get("credentialUid") != entry["credentialUid"]
            or not isinstance(nodes, list)
            or {node.get("id") for node in nodes}
            != {f"database:{entry['logicalUid']}"}
            | {f"database:{entry['logicalUid']}:{item['uid']}" for item in instances}
            or not any(condition.get("conditionType") == "Ready"
                       and condition.get("status") == "True"
                       for condition in entry["conditions"])
        ):
            raise RuntimeError(f"catalog entry is not independently Ready: {entry['name']}")
        result[entry["name"]] = entry
    if len({entry["namespaceUid"] for entry in entries}) != len(entries) or len(
        {entry["clusterUid"] for entry in entries}
    ) != len(entries):
        raise RuntimeError("catalog entry infrastructure identities overlap")
    for identity in ("pvUid", "pvcUid", "diskUid"):
        values = [
            item[identity] for entry in entries for item in entry["storage"]
            if item[identity] is not None
        ]
        if len(values) != len(set(values)):
            raise RuntimeError(f"catalog entry {identity} identities overlap")
    pod_uids = [item["uid"] for entry in entries for item in entry["instanceTopology"]]
    if len(pod_uids) != len(set(pod_uids)):
        raise RuntimeError("catalog instance identities overlap")
    return result


class CatalogClient:
    def __init__(self, get: Get, mutate: Mutate, tenant: str, tenant_uid: str):
        self.get = get
        self.mutate = mutate
        self.tenant = tenant
        self.tenant_uid = tenant_uid

    @property
    def path(self) -> str:
        return f"api/v1/tenants/{self.tenant}/databases"

    def read(self, expected_uid: str | None = None) -> dict[str, object]:
        return validate_catalog(
            envelope(self.get(self.path), "database list"),
            self.tenant, self.tenant_uid, expected_uid=expected_uid,
        )

    def request(self, method: str, path: str, payload: dict[str, object],
                expected_uid: str) -> dict[str, object]:
        response = self.mutate(method, path, payload)
        if response.returncode:
            raise RuntimeError(f"catalog {method} {path} failed: {response.stderr}")
        return validate_catalog(
            envelope(response.stdout, f"catalog {method}"),
            self.tenant, self.tenant_uid, expected_uid=expected_uid,
        )

    def add(self, catalog_uid: str, name: str) -> str:
        before = self.read(catalog_uid)
        if any(entry["name"] == name for entry in before["databases"]):
            raise RuntimeError(f"catalog entry already exists: {name}")
        result = self.request(
            "POST", self.path,
            {"catalogUid": catalog_uid, "name": name, "instances": 3},
            catalog_uid,
        )
        added = [entry for entry in result["databases"] if entry["name"] == name]
        if len(added) != 1 or added[0]["instances"] != 3:
            raise RuntimeError("catalog add did not return the requested entry")
        return added[0]["logicalUid"]

    def delete(self, catalog_uid: str, logical_uid: str, name: str) -> None:
        result = self.request(
            "DELETE", f"{self.path}/{logical_uid}",
            {"catalogUid": catalog_uid, "logicalUid": logical_uid, "confirmation": name},
            catalog_uid,
        )
        matches = [entry for entry in result["databases"] if entry["logicalUid"] == logical_uid]
        if matches and (len(matches) != 1 or matches[0]["deleting"] is not True):
            raise RuntimeError("catalog deletion intent was not recorded")

    def require_stale_query(self, catalog_uid: str, entry: Mapping[str, object]) -> None:
        instance = entry["instanceTopology"][0]
        require_stale_identity(self.mutate(
            "POST", f"{self.path}/{entry['logicalUid']}/query", {
                "catalogUid": catalog_uid, "logicalUid": entry["logicalUid"],
                "instance": instance["name"], "instanceUid": instance["uid"],
                "database": "postgres", "sql": "SELECT 1",
            },
        ))

    def probe(self, catalog_uid: str, entry: Mapping[str, object]) -> None:
        primary = [
            item for item in entry["instanceTopology"]
            if item["role"] == "primary" and item["ready"] is True
        ]
        if len(primary) != 1:
            raise RuntimeError("catalog query primary is unavailable")
        instance = primary[0]
        response = self.mutate("POST", f"{self.path}/{entry['logicalUid']}/query", {
            "catalogUid": catalog_uid, "logicalUid": entry["logicalUid"],
            "instance": instance["name"], "instanceUid": instance["uid"],
            "database": "postgres", "sql": "SELECT 1 AS value",
        })
        if response.returncode:
            raise RuntimeError(f"catalog query probe failed: {response.stderr}")
        result = envelope(response.stdout, "catalog query probe")
        if (
            set(result) != {
                "catalogUid", "logicalUid", "instance", "instanceUid",
                "executedAt", "durationMs", "truncated", "results",
            }
            or result["catalogUid"] != catalog_uid
            or result["logicalUid"] != entry["logicalUid"]
            or result["instance"] != instance["name"]
            or result["instanceUid"] != instance["uid"]
            or result["truncated"] is not False
            or result["results"] != [{
                "columns": ["value"], "rows": [["1"]],
                "affectedRows": 1, "truncated": False,
            }]
        ):
            raise RuntimeError("catalog query probe identity or result differs")

    def query(self, catalog_uid: str, entry: Mapping[str, object],
              marker: str, *, write: bool = False) -> None:
        primary = [
            item for item in entry["instanceTopology"]
            if item["role"] == "primary" and item["ready"] is True
        ]
        if len(primary) != 1 or not re.fullmatch(r"marker-[a-z]+(?:-[a-z]+)*", marker):
            raise RuntimeError("catalog query primary or marker is invalid")
        instance = primary[0]
        path = f"{self.path}/{entry['logicalUid']}/query"
        sql = (
            "CREATE TABLE IF NOT EXISTS lifecycle_marker(value text PRIMARY KEY);"
            f"INSERT INTO lifecycle_marker(value) VALUES ('{marker}') ON CONFLICT DO NOTHING;"
            if write else
            "SELECT value FROM lifecycle_marker LIMIT 1"
        )
        response = self.mutate("POST", path, {
            "catalogUid": catalog_uid, "logicalUid": entry["logicalUid"],
            "instance": instance["name"], "instanceUid": instance["uid"],
            "database": "postgres", "sql": sql,
        })
        if response.returncode:
            raise RuntimeError(f"catalog query failed: {response.stderr}")
        result = envelope(response.stdout, "catalog query")
        if (
            set(result) != {
                "catalogUid", "logicalUid", "instance", "instanceUid",
                "executedAt", "durationMs", "truncated", "results",
            }
            or result["catalogUid"] != catalog_uid
            or result["logicalUid"] != entry["logicalUid"]
            or result["instance"] != instance["name"]
            or result["instanceUid"] != instance["uid"]
            or result["truncated"] is not False
            or not isinstance(result["results"], list)
            or (not write and result["results"] != [{
                "columns": ["value"], "rows": [[marker]],
                "affectedRows": 1, "truncated": False,
            }])
            or (write and not result["results"])
        ):
            raise RuntimeError("catalog query identity or marker differs")

    def assert_fresh(self, catalog_uid: str, entry: Mapping[str, object]) -> None:
        primary = [
            item for item in entry["instanceTopology"]
            if item["role"] == "primary" and item["ready"] is True
        ]
        if len(primary) != 1:
            raise RuntimeError("recreated database primary is unavailable")
        instance = primary[0]
        response = self.mutate("POST", f"{self.path}/{entry['logicalUid']}/query", {
            "catalogUid": catalog_uid, "logicalUid": entry["logicalUid"],
            "instance": instance["name"], "instanceUid": instance["uid"],
            "database": "postgres",
            "sql": "SELECT to_regclass('public.lifecycle_marker') IS NULL AS fresh",
        })
        if response.returncode:
            raise RuntimeError(f"recreated database probe failed: {response.stderr}")
        result = envelope(response.stdout, "database recreation probe")
        if (
            set(result) != {
                "catalogUid", "logicalUid", "instance", "instanceUid",
                "executedAt", "durationMs", "truncated", "results",
            }
            or result["catalogUid"] != catalog_uid
            or result["logicalUid"] != entry["logicalUid"]
            or result["instance"] != instance["name"]
            or result["instanceUid"] != instance["uid"]
            or result["truncated"] is not False
            or result["results"] != [{
                "columns": ["fresh"], "rows": [["t"]],
                "affectedRows": 1, "truncated": False,
            }]
        ):
            raise RuntimeError("recreated database retained old entry data")

    def wait(self, predicate: Callable[[dict[str, object]], bool],
             timeout: int, expected_uid: str) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            result = self.read(expected_uid)
            if predicate(result):
                return result
            if time.monotonic() >= deadline:
                raise RuntimeError("catalog did not converge before deadline")
            time.sleep(5)
