from __future__ import annotations

import re
from pathlib import Path

from .files import IntegrityError, write_private_file


PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*):=([^}]*)\}")


def render_release_manifest(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, str],
    variables: dict[str, str],
) -> None:
    content = source.read_text(encoding="utf-8")
    for tagged, pinned in replacements.items():
        count = content.count(tagged)
        if count != 1:
            raise IntegrityError(
                f"{source.name} contains {count} occurrences of {tagged}, expected one"
            )
        content = content.replace(tagged, pinned)

    def replace_variable(match: re.Match[str]) -> str:
        name, default = match.groups()
        return variables.get(name, default)

    content = PLACEHOLDER.sub(replace_variable, content)
    if "${" in content:
        raise IntegrityError(f"unresolved release variable remains in {source.name}")
    write_private_file(destination, content)


def replace_known_images(content: str, config: dict[str, str]) -> str:
    pairs = (
        ("KAMAJI_IMAGE_TAGGED", "KAMAJI_IMAGE"),
        ("KAMAJI_ETCD_IMAGE_TAGGED", "KAMAJI_ETCD_IMAGE"),
        ("KAMAJI_ETCD_JOB_IMAGE_TAGGED", "KAMAJI_ETCD_JOB_IMAGE"),
        ("KAMAJI_KUBECTL_JOB_IMAGE_TAGGED", "KAMAJI_KUBECTL_JOB_IMAGE"),
    )
    result = content
    for tagged_key, pinned_key in pairs:
        tagged = config[tagged_key]
        if tagged in result:
            result = result.replace(tagged, config[pinned_key])
    return result
