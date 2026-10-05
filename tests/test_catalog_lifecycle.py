from __future__ import annotations

import copy
import json
import unittest
from subprocess import CompletedProcess
from unittest.mock import Mock

from scripts.lib.catalog_lifecycle import (
    CatalogClient, envelope, ready_entries, require_stale_identity, validate_catalog,
)


CATALOG = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FIRST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SECOND = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
THIRD = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def view(entries=None):
    return {
        "tenant": "tenant-a", "tenantUid": "tenant-uid",
        "catalogUid": CATALOG, "resourceVersion": "42",
        "closed": False, "capabilityAvailable": True,
        "databases": [] if entries is None else entries,
    }


def entry(name, uid):
    instances = [
        {"name": f"pg-{name}-{i}", "uid": f"pod-{name}-{i}",
         "role": "primary" if i == 1 else "standby", "ready": True}
        for i in (1, 2, 3)
    ]
    return {
        "logicalUid": uid, "name": name, "instances": 3,
        "deleting": False, "phase": "ready", "observedGeneration": 4,
        "provider": "local", "namespace": f"db-{name}",
        "namespaceUid": f"ns-{name}", "cluster": f"pg-{name}",
        "clusterUid": f"cluster-{name}", "credentialUid": f"secret-{name}",
        "queryIdentity": {
            "clusterUid": f"cluster-{name}", "credentialUid": f"secret-{name}",
        },
        "readyInstances": 3, "storageRequestedBytes": 3221225472,
        "storageHealthy": 3,
        "storage": [
            {"ordinal": i, "requestedBytes": 1073741824,
             "healthy": True, "pvUid": f"pv-{name}-{i}",
             "pvcUid": f"pvc-{name}-{i}", "diskUid": None}
            for i in (1, 2, 3)
        ],
        "conditions": [{"conditionType": "Ready", "status": "True"}],
        "finalization": None, "instanceTopology": instances, "blockers": [],
        "topology": {
            "nodes": [{"id": f"database:{uid}"}]
            + [{"id": f"database:{uid}:{instance['uid']}"} for instance in instances],
        },
    }


class CatalogLifecycleTests(unittest.TestCase):
    def test_stale_identity_must_be_exact_schema_v5_conflict(self):
        expected = CompletedProcess([], 1, json.dumps({
            "schemaVersion": 7, "error": {
                "code": "stale-identity", "message": "stale", "retryable": False,
            },
        }), "HTTP 409: Conflict")
        require_stale_identity(expected)
        for changed in (
            {"returncode": 0}, {"stderr": "HTTP 403: Forbidden"},
            {"stdout": json.dumps({"schemaVersion": 4, "error": {
                "code": "stale-identity", "retryable": False,
            }})},
        ):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                require_stale_identity(CompletedProcess(
                    [], changed.get("returncode", expected.returncode),
                    changed.get("stdout", expected.stdout),
                    changed.get("stderr", expected.stderr),
                ))

    def test_exact_schema_and_tenant_identity(self):
        response = {"schemaVersion": 7, "data": view()}
        self.assertEqual(view(), validate_catalog(
            envelope(json.dumps(response), "catalog"), "tenant-a", "tenant-uid",
        ))
        for bad in (
            {"schemaVersion": 4, "data": view()},
            {"schemaVersion": 7, "data": []},
            {"schemaVersion": 7, "data": view(), "password": "secret"},
        ):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                envelope(json.dumps(bad), "catalog")
        for changed in (
            {"tenantUid": "successor"}, {"catalogUid": "bad"},
            {"resourceVersion": ""}, {"closed": None},
        ):
            invalid = view() | changed
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                validate_catalog(invalid, "tenant-a", "tenant-uid")

    def test_three_ready_entries_have_independent_topology_and_storage(self):
        values = [entry("alpha", FIRST), entry("beta", SECOND), entry("gamma", THIRD)]
        self.assertEqual(
            {"alpha", "beta", "gamma"},
            set(ready_entries(view(values), {"alpha", "beta", "gamma"}, "local")),
        )
        for path, value in (
            (("storageHealthy",), 2),
            (("phase",), "progressing"),
            (("namespaceUid",), "ns-alpha"),
            (("topology", "nodes"), []),
            (("instanceTopology",), []),
            (("storage",), [*values[0]["storage"]]),
        ):
            with self.subTest(path=path):
                invalid = copy.deepcopy(values)
                target = invalid[1]
                if len(path) == 2:
                    target[path[0]][path[1]] = value
                else:
                    target[path[0]] = value
                with self.assertRaises(RuntimeError):
                    ready_entries(view(invalid), {"alpha", "beta", "gamma"}, "local")

    def test_add_delete_query_bind_exact_catalog_and_logical_uid(self):
        initial = view()
        added = view([entry("alpha", FIRST)])
        deleted = view([{**entry("alpha", FIRST), "deleting": True}])
        responses = iter((added, deleted))
        mutate = Mock(side_effect=lambda *_: CompletedProcess(
            [], 0, json.dumps({"schemaVersion": 7, "data": next(responses)}), "",
        ))
        client = CatalogClient(
            lambda _: json.dumps({"schemaVersion": 7, "data": initial}),
            mutate, "tenant-a", "tenant-uid",
        )
        self.assertEqual(FIRST, client.add(CATALOG, "alpha"))
        client.delete(CATALOG, FIRST, "alpha")
        self.assertEqual(FIRST, mutate.call_args.args[2]["logicalUid"])
        with self.assertRaisesRegex(RuntimeError, "catalog UID changed"):
            client.read(SECOND)

    def test_read_only_probe_binds_primary_and_exact_result(self):
        value = entry("alpha", FIRST)
        result = {
            "catalogUid": CATALOG, "logicalUid": FIRST,
            "instance": "pg-alpha-1", "instanceUid": "pod-alpha-1",
            "executedAt": "2026-10-01T00:00:00Z", "durationMs": 2,
            "truncated": False, "results": [{
                "columns": ["value"], "rows": [["1"]],
                "affectedRows": 1, "truncated": False,
            }],
        }
        mutate = Mock(side_effect=lambda *_: CompletedProcess(
            [], 0, json.dumps({"schemaVersion": 7, "data": result}), "",
        ))
        client = CatalogClient(lambda _: "", mutate, "tenant-a", "tenant-uid")
        client.probe(CATALOG, value)
        self.assertEqual("SELECT 1 AS value", mutate.call_args.args[2]["sql"])
        self.assertEqual("pod-alpha-1", mutate.call_args.args[2]["instanceUid"])
        result["logicalUid"] = SECOND
        with self.assertRaisesRegex(RuntimeError, "identity or result differs"):
            client.probe(CATALOG, value)
