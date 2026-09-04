from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .files import write_private_file


class OwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class IdentityRecord:
    kind: str
    name: str
    identifier: str
    labels: dict[str, str]

    def save(self, path: Path) -> None:
        write_private_file(
            path,
            json.dumps(
                {
                    "identifier": self.identifier,
                    "kind": self.kind,
                    "labels": self.labels,
                    "name": self.name,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )

    @classmethod
    def load(cls, path: Path) -> "IdentityRecord":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                kind=payload["kind"],
                name=payload["name"],
                identifier=payload["identifier"],
                labels=dict(payload["labels"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OwnershipError(f"malformed ownership record: {path}") from exc

    def require_exact(self, observed: "IdentityRecord") -> None:
        if self != observed:
            raise OwnershipError(
                f"ownership mismatch for {self.kind} {self.name}: "
                f"expected {self.identifier}, observed {observed.identifier}"
            )
