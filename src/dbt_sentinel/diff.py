"""Parse a unified diff into changed files, then join them to dbt nodes.

Scope note: column extraction from SQL is a heuristic, not a parser. It catches the
common `select ... as alias` and bare-column cases and is wrong on dynamic SQL, macros
that emit columns, and `select *`. Those cases are surfaced as low-confidence rather
than silently dropped — see `Day 4` for the sqlglot-based replacement.
"""

from __future__ import annotations

import re

from .lineage import Lineage
from .models import ChangedFile, ChangedNode, ChangeType, Node

_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")
_NEW_FILE = re.compile(r"^new file mode")
_DELETED_FILE = re.compile(r"^deleted file mode")
_RENAME_FROM = re.compile(r"^rename from (?P<path>.+)$")
_HUNK = re.compile(r"^@@ ")

# `foo as bar`, `foo AS bar` -> bar
_SQL_ALIAS = re.compile(r"\bas\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:,|$)", re.IGNORECASE)
# `- name: customer_id` in a schema.yml
_YAML_COLUMN = re.compile(r"^\s*-\s*name:\s*['\"]?([a-zA-Z_][a-zA-Z0-9_]*)['\"]?\s*$")

_DBT_EXTENSIONS = (".sql", ".yml", ".yaml", ".csv", ".py")


def parse_diff(diff_text: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    current: dict | None = None

    def flush() -> None:
        if current is None:
            return
        files.append(
            ChangedFile(
                path=current["path"],
                change_type=current["change_type"],
                added_lines=tuple(current["added"]),
                removed_lines=tuple(current["removed"]),
                previous_path=current.get("previous_path"),
                hunk_lines=tuple(current["hunk"]),
            )
        )

    for line in diff_text.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            flush()
            current = {
                "path": header.group("b"),
                "change_type": ChangeType.MODIFIED,
                "added": [],
                "removed": [],
                "hunk": [],
            }
            if header.group("a") != header.group("b"):
                current["change_type"] = ChangeType.RENAMED
                current["previous_path"] = header.group("a")
            continue

        if current is None:
            continue

        if _NEW_FILE.match(line):
            current["change_type"] = ChangeType.ADDED
        elif _DELETED_FILE.match(line):
            current["change_type"] = ChangeType.DELETED
        elif rename := _RENAME_FROM.match(line):
            current["change_type"] = ChangeType.RENAMED
            current["previous_path"] = rename.group("path")
        elif _HUNK.match(line):
            continue
        # `+++ b/path` and `--- a/path` must not be read as content lines.
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith(("+", "-", " ")):
            current["hunk"].append(line)
            if line.startswith("+"):
                current["added"].append(line[1:])
            elif line.startswith("-"):
                current["removed"].append(line[1:])

    flush()
    return [f for f in files if f.path.endswith(_DBT_EXTENSIONS)]


def extract_columns(lines: tuple[str, ...], is_yaml: bool) -> set[str]:
    """Best-effort column names from a set of diff lines."""
    found: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("--", "#", "{#")):
            continue
        if is_yaml:
            if match := _YAML_COLUMN.match(line):
                found.add(match.group(1).lower())
        else:
            found.update(m.lower() for m in _SQL_ALIAS.findall(stripped))
    return found


def yaml_columns_by_model(hunk_lines: tuple[str, ...]) -> dict[str, tuple[set[str], set[str]]]:
    """Scope column edits in a schema.yml to the model block they sit under.

    Context lines from the diff are what make this possible: a `+ - name: cust_id` line
    on its own is unattributable, but the surrounding unchanged lines tell us which
    model's `columns:` list it belongs to. Without this, editing one model in a shared
    schema.yml flags every model in the file that happens to share a column name.
    """
    result: dict[str, tuple[set[str], set[str]]] = {}
    current_model: str | None = None
    model_indent = -1
    in_columns = False

    for raw in hunk_lines:
        prefix, line = raw[0], raw[1:]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped.startswith("columns:"):
            in_columns = True
            continue

        match = _YAML_COLUMN.match(line)
        if not match:
            if stripped and indent <= model_indent:
                in_columns = False
            continue

        name = match.group(1)
        if not in_columns or (model_indent >= 0 and indent <= model_indent):
            # A `- name:` at or above model indent is a model heading, not a column.
            current_model, model_indent, in_columns = name, indent, False
            result.setdefault(name, (set(), set()))
            continue

        if current_model is None:
            continue
        added, removed = result.setdefault(current_model, (set(), set()))
        if prefix == "+":
            added.add(name.lower())
        elif prefix == "-":
            removed.add(name.lower())

    return result


def _is_semantic(lines: tuple[str, ...], is_yaml: bool) -> bool:
    """Ignore blank lines and comment-only edits."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if is_yaml and stripped.startswith("#"):
            continue
        if not is_yaml and stripped.startswith(("--", "#", "{#")):
            continue
        return True
    return False


def resolve_changes(diff_text: str, lineage: Lineage) -> tuple[list[ChangedNode], list[ChangedFile]]:
    """Returns (nodes we could resolve, files we could not).

    Unresolved files matter: a changed macro or a new model absent from a stale manifest
    is exactly the case where staying quiet is dangerous. They are returned so the caller
    can warn rather than silently under-report the blast radius.
    """
    resolved: list[ChangedNode] = []
    unresolved: list[ChangedFile] = []

    seen: set[str] = set()

    for changed in parse_diff(diff_text):
        nodes: list[Node] = lineage.nodes_by_file_path(changed.path)
        if not nodes and changed.previous_path:
            nodes = lineage.nodes_by_file_path(changed.previous_path)
        if not nodes:
            unresolved.append(changed)
            continue

        is_yaml = changed.path.endswith((".yml", ".yaml"))
        semantic = _is_semantic(changed.added_lines + changed.removed_lines, is_yaml)
        per_model = yaml_columns_by_model(changed.hunk_lines) if is_yaml else {}

        for node in nodes:
            if is_yaml:
                if node.name not in per_model:
                    continue
                added, removed = per_model[node.name]
            else:
                added = extract_columns(changed.added_lines, False)
                removed = extract_columns(changed.removed_lines, False)

            if node.unique_id in seen:
                continue
            seen.add(node.unique_id)

            resolved.append(
                ChangedNode(
                    node=node,
                    change_type=changed.change_type,
                    file=changed,
                    added_columns=tuple(sorted(added - removed)),
                    removed_columns=tuple(sorted(removed - added)),
                    has_semantic_change=semantic,
                )
            )

    return resolved, unresolved
