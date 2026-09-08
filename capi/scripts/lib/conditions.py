from __future__ import annotations

from typing import Any

from .redaction import redact

def condition(resource: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in resource.get("status", {}).get("conditions", []) or []
            if item.get("type") == condition_type
        ),
        None,
    )


def condition_true(resource: dict[str, Any], condition_type: str) -> bool:
    item = condition(resource, condition_type)
    return bool(
        item
        and item.get("status") == "True"
        and isinstance(item.get("observedGeneration"), int)
        and item.get("observedGeneration")
        == resource.get("metadata", {}).get("generation")
    )


def condition_summary(resource: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": item.get("type"),
            "status": item.get("status"),
            "reason": item.get("reason"),
            "message": item.get("message"),
            "observedGeneration": item.get("observedGeneration"),
        }
        for item in resource.get("status", {}).get("conditions", []) or []
    ]


def sanitized_condition_summary(
    resource: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            **item,
            "reason": (
                redact(str(item["reason"]))
                if item.get("reason") is not None
                else None
            ),
            "message": (
                redact(str(item["message"]))
                if item.get("message") is not None
                else None
            ),
        }
        for item in condition_summary(resource)
    ]
