#!/usr/bin/env python3

import json
import os
import re
import sys


def cpu(value):
    if not value:
        return 0
    if value.endswith("n"):
        return int(value[:-1])
    if value.endswith("u"):
        return int(value[:-1]) * 1_000
    if value.endswith("m"):
        return int(value[:-1]) * 1_000_000
    return int(value) * 1_000_000_000


def memory(value):
    if not value:
        return 0
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
    }
    match = re.fullmatch(r"([0-9]+)([A-Za-z]+)?", value)
    if not match:
        raise ValueError(f"unsupported memory request: {value}")
    return int(match.group(1)) * units.get(match.group(2), 1)


def request(container, resource, parser):
    return parser(
        container.get("resources", {}).get("requests", {}).get(resource, "0")
    )


def effective(pod, resource, parser):
    spec = pod.get("spec", {})
    application = sum(
        request(container, resource, parser)
        for container in spec.get("containers", [])
    )
    running_sidecars = 0
    init_peak = 0
    for container in spec.get("initContainers", []):
        container_request = request(container, resource, parser)
        if container.get("restartPolicy") == "Always":
            running_sidecars += container_request
        else:
            init_peak = max(init_peak, running_sidecars + container_request)
    steady_state = application + running_sidecars
    overhead = parser(spec.get("overhead", {}).get(resource, "0"))
    return max(steady_state, init_peak) + overhead


pods = [
    pod
    for pod in json.load(sys.stdin).get("items", [])
    if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
]
requested_cpu = sum(effective(pod, "cpu", cpu) for pod in pods)
requested_memory = sum(effective(pod, "memory", memory) for pod in pods)
assert requested_cpu <= int(os.environ["CPU_CAP"])
assert requested_memory <= int(os.environ["MEMORY_CAP"])
print(f"REQUESTED_CPU_NANO={requested_cpu}")
print(f"REQUESTED_MEMORY_BYTES={requested_memory}")
