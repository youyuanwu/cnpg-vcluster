from __future__ import annotations

import re
from collections.abc import Sequence


CLI_SECRET = re.compile(
    r"(?i)(--(?:token|password|pgpassword|client-key-data|"
    r"client-certificate-data|certificate-authority-data|kubeadm-token))"
    r"(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|\S+)"
)
CLI_SECRET_NAMES = {
    "--token",
    "--password",
    "--pgpassword",
    "--client-key-data",
    "--client-certificate-data",
    "--certificate-authority-data",
    "--kubeadm-token",
}

PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\b(token|password|pgpassword|client-key-data|client-certificate-data)\s*[:=]\s*\S+"),
    re.compile(r"\b[a-z0-9]{6}\.[a-z0-9]{16}\b", re.IGNORECASE),
    re.compile(r"(?i)\bAuthorization:\s*\S+(?:\s+\S+)?"),
)


def redact(value: str) -> str:
    result = CLI_SECRET.sub(lambda match: f"{match.group(1)}=REDACTED", value)
    for pattern in PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda match: f"{match.group(1)}=REDACTED", result)
        else:
            result = pattern.sub("REDACTED", result)
    return result


def redact_argv(arguments: Sequence[str]) -> str:
    redacted: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        lowered = argument.lower()
        if lowered in CLI_SECRET_NAMES:
            redacted.extend((argument, "REDACTED"))
            index += 2
            continue
        matched_assignment = next(
            (
                name
                for name in CLI_SECRET_NAMES
                if lowered.startswith(f"{name}=")
            ),
            None,
        )
        if matched_assignment:
            redacted.append(f"{argument.split('=', 1)[0]}=REDACTED")
        else:
            redacted.append(redact(argument))
        index += 1
    return " ".join(redacted)
