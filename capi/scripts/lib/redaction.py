from __future__ import annotations

import re


CLI_SECRET = re.compile(
    r"(?i)(--(?:token|password|pgpassword|client-key-data|"
    r"client-certificate-data|certificate-authority-data|kubeadm-token))"
    r"(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|\S+)"
)

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
