"""Exposed-secret scanning: the one finding outside the blast radius that gates the merge.

The spec is the Security section of [docs/CHECKS.md](../../docs/CHECKS.md).

Not a check, and deliberately not in `checks.CHECKS`. Checks are capped at medium
because a lint finding costs something only if merged. A credential is compromised the
moment it is pushed, because it's then in git history, forks and CI logs, so waiting on
the merge or weighting it by reach would both be wrong. It gets its own type so the
check layer's cap stays an invariant rather than a rule with one exception.

Deterministic by design rule 1: credential formats are exact patterns, and a missed
secret is the costliest miss this tool can make, so it must not depend on sampling.

Like the checks, it reads added lines only. Unlike them, it reads every file, not just
`models/**`: secrets leak through profiles.yml, dbt_project.yml, macros and `.env`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ChangedFile

CHECK_ID = "exposed-secret"

# Provider formats, most specific first: an Anthropic key also matches the OpenAI shape,
# and the first match on a line names it.
_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe live key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
)

# A URL carrying `user:password@`. The password group is checked against the same
# placeholder rules as an assignment, so `postgres://u:${PGPASSWORD}@h` stays silent.
_URL_CREDENTIALS = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:([^\s@/'\"]+)@", re.IGNORECASE)

# A credential-named key assigned a value: `password: x`, `"api_key": "x"`,
# `aws_secret_key = 'x'`. The key may carry a prefix or suffix (`db_password`).
_ASSIGNMENT = re.compile(
    r"""["']?(?P<key>[A-Za-z0-9_.-]*"""
    r"""(?:password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key|access[_-]?key)"""
    r"""[A-Za-z0-9_.-]*)["']?\s*[:=]\s*(?P<value>.+)""",
    re.IGNORECASE,
)

# Keys that name *about* a credential rather than holding one: `private_key_path` is a
# file location, `token_type` is `bearer`, `password_hash` is not reversible.
_DESCRIPTIVE_KEY = re.compile(r"(?:_|-)(?:path|file|name|type|hash|env|var|at|expires?)$", re.IGNORECASE)

_PLACEHOLDER = re.compile(
    r"^(?:<[^>]*>|\*+|x{3,}|changeme|change_me|redacted|todo|example|your[_-].*|.*example.*|null|none|~|true|false)$",
    re.IGNORECASE,
)

# Files where an unquoted value is still a literal. In code, `password = os.environ[...]`
# reads a secret rather than exposing one, so there only a quoted literal counts.
_CONFIG_SUFFIXES = (".yml", ".yaml", ".env", ".ini", ".cfg", ".toml", ".properties", ".conf")


@dataclass(frozen=True)
class SecretFinding:
    """One exposed credential. `preview` is redacted: it's the only part rendered."""

    kind: str
    path: str
    preview: str
    check_id: str = CHECK_ID


def _is_config(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.endswith(_CONFIG_SUFFIXES) or name.startswith(".env")


def _literal(raw: str, config: bool) -> str | None:
    """The literal a credential key is assigned, or None if it's a reference or empty."""
    # Jinja first: `"{{ env_var('DBT_PASSWORD') }}"` must reduce to nothing, not to the
    # quote characters around it.
    value = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", raw).strip().rstrip(",;")
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end > 0 else value[1:]
    elif config:
        value = value.split(" #", 1)[0].strip()
    else:
        return None
    if not value or "${" in value or value.startswith("$") or _PLACEHOLDER.match(value):
        return None
    return value


def _redact(value: str, keep: int) -> str:
    return (value[:keep] + "…" if keep else "") + "****"


def scan_file(file: ChangedFile) -> list[SecretFinding]:
    config = _is_config(file.path)
    found: dict[str, SecretFinding] = {}

    for line in file.added_lines:
        line = line.rstrip("\n")

        for kind, pattern in _FORMATS:
            if match := pattern.search(line):
                # A provider prefix like `AKIA` is public knowledge, so keeping it makes
                # the finding identifiable without revealing anything usable.
                found.setdefault(kind, SecretFinding(kind, file.path, _redact(match.group(0), 4)))
                break
        else:
            if (url := _URL_CREDENTIALS.search(line)) and _literal(url.group(1), True):
                found.setdefault(
                    "password in a connection URL",
                    SecretFinding("password in a connection URL", file.path, _redact("", 0)),
                )
                continue
            assignment = _ASSIGNMENT.search(line)
            if assignment is None or _DESCRIPTIVE_KEY.search(assignment.group("key")):
                continue
            if _literal(assignment.group("value"), config) is not None:
                kind = f"literal value for `{assignment.group('key')}`"
                # Nothing of a password is shown: even four characters of an eight
                # character password is half of it.
                found.setdefault(kind, SecretFinding(kind, file.path, _redact("", 0)))

    return list(found.values())


def scan_secrets(files: list[ChangedFile]) -> list[SecretFinding]:
    """Every exposed credential in the added lines of every file, ordered by path."""
    findings = [finding for file in files for finding in scan_file(file)]
    return sorted(findings, key=lambda f: (f.path, f.kind))
