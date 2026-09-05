from __future__ import annotations

import json
from pathlib import Path

from scripts.lib.kube import ManagementClient
from scripts.status import collect_status


def diagnose(root: Path, config: dict[str, str], scope: str) -> int:
    if scope not in {"all", "management"}:
        raise RuntimeError("Phase 2 diagnostics support only all or management")
    result = collect_status(root, config)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["management"].get("apiReady"):
        return 1
    client = ManagementClient(root, config)
    for arguments in (
        ("get", "deployments,statefulsets,daemonsets", "-A", "-o", "wide"),
        ("get", "crd", "-o", "name"),
        ("get", "events", "-A", "--sort-by=.lastTimestamp"),
    ):
        response = client.kubectl(*arguments, check=False)
        print(f"\n$ kubectl {' '.join(arguments)}")
        print(response.stdout)
        if response.stderr:
            print(response.stderr)
    return 0
