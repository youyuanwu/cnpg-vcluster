from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .redaction import redact


CLASSIFICATIONS = frozenset(
    {
        "ready",
        "progressing",
        "degraded",
        "failed",
        "deleting",
        "absent",
        "ownership-invalid",
    }
)


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


@dataclass(frozen=True)
class TenantStatus:
    profile: str
    tenant: str
    classification: str
    foundation_healthy: bool
    components: Mapping[str, object] = field(default_factory=dict)
    blockers: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise ValueError(
                f"unsupported tenant lifecycle classification: {self.classification}"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": 1,
            "profile": self.profile,
            "tenant": self.tenant,
            "classification": self.classification,
            "foundationHealthy": self.foundation_healthy,
            "components": _redact_value(self.components),
            "blockers": _redact_value(self.blockers),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_mapping(), sort_keys=True)
