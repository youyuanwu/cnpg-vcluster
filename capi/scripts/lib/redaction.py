from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence


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
    re.compile(
        r"(?i)\b(subscription[_-]?id|client[_-]?secret|kubeconfig)\s*[:=]\s*\S+"
    ),
)
AZURE_SUBSCRIPTION_PATH = re.compile(
    r"(?i)(/subscriptions/)[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SERIALIZED_SECRET = re.compile(
    r"""(?ix)
    (
      ["']?
      (?:token|password|pgpassword|client[_-]?secret|subscription[_-]?id|
         client[_-]?key[_-]?data|client[_-]?certificate[_-]?data|
         certificate[_-]?authority[_-]?data|kubeconfig)
      ["']?
      \s*[:=]\s*
    )
    (?:
      "(?:\\.|[^"\\])*"
      |
      '(?:\\.|[^'\\])*'
      |
      [^,\s}\]]+
    )
    """
)
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "certificateauthoritydata",
        "clientcertificatedata",
        "clientkeydata",
        "clientsecret",
        "kubeadmtoken",
        "kubeconfig",
        "password",
        "pgpassword",
        "subscriptionid",
        "token",
    }
)


def _redact_patterns(value: str) -> str:
    result = CLI_SECRET.sub(lambda match: f"{match.group(1)}=REDACTED", value)
    result = SERIALIZED_SECRET.sub(
        lambda match: f"{match.group(1)}\"REDACTED\"",
        result,
    )
    for pattern in PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda match: f"{match.group(1)}=REDACTED", result)
        else:
            result = pattern.sub("REDACTED", result)
    return AZURE_SUBSCRIPTION_PATH.sub(
        lambda match: f"{match.group(1)}REDACTED",
        result,
    )


def _redact_value(
    value: object,
    *,
    key: str | None = None,
    depth: int,
) -> object:
    normalized = "" if key is None else re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized in SENSITIVE_KEYS:
        return "REDACTED"
    if isinstance(value, str):
        return _redact_text(value, depth=depth + 1)
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(
                item,
                key=str(item_key),
                depth=depth + 1,
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, depth=depth + 1) for item in value]
    return value


def _redact_text(value: str, *, depth: int) -> str:
    if depth > 8:
        return _redact_patterns(value)
    stripped = value.strip()
    if stripped.startswith(("{", "[", '"')):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, (dict, list, str)):
                return json.dumps(
                    _redact_value(parsed, depth=depth + 1),
                    sort_keys=True,
                    separators=(",", ":"),
                )
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character not in "{[":
            continue
        try:
            parsed, length = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, (dict, list)):
            continue
        sanitized = json.dumps(
            _redact_value(parsed, depth=depth + 1),
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            _redact_patterns(value[:index])
            + sanitized
            + _redact_text(value[index + length :], depth=depth + 1)
        )
    return _redact_patterns(value)


def redact(value: str) -> str:
    return _redact_text(value, depth=0)


def redact_value(value: object, *, key: str | None = None) -> object:
    return _redact_value(value, key=key, depth=0)


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
