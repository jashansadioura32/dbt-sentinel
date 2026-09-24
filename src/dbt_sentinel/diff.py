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
    result, _ = _scan_yaml_hunk(hunk_lines)
    return result


def yaml_orphan_columns(hunk_lines: tuple[str, ...]) -> tuple[set[str], set[str]]:
    """Column edits in a schema.yml that belong to no discoverable model heading.

    Returns (added, removed). A mid-file hunk inside a `columns:` list shows the column
    names but not the model that owns them. Callers surface these as an uncertainty
    rather than attributing them to every node in the file.
    """
    _, orphans = _scan_yaml_hunk(hunk_lines)
    return orphans


def _scan_yaml_hunk(
    hunk_lines: tuple[str, ...],
) -> tuple[dict[str, tuple[set[str], set[str]]], tuple[set[str], set[str]]]:
    result: dict[str, tuple[set[str], set[str]]] = {}
    orphan_added: set[str] = set()
    orphan_removed: set[str] = set()
    current_model: str | None = None
    model_indent = -1
    columns_indent = -1
    in_columns = False
    last_prefix = " "
    # A `tests:`/`data_tests:` list holds `- name:` entries too, but they name tests,
    # not columns. Tracked separately from `in_columns` because a tests: block sits
    # *inside* a column entry, so leaving it must restore the enclosing columns state.
    tests_indent = -1

    for raw in hunk_lines:
        prefix, line = raw[0], raw[1:]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if not stripped:
            continue

        # Dedent out of a tests: block before anything else looks at this line.
        if tests_indent >= 0 and indent <= tests_indent:
            tests_indent = -1

        if stripped.startswith(("tests:", "data_tests:")):
            tests_indent = indent
            continue

        if stripped.startswith("columns:"):
            in_columns = True
            columns_indent = indent
            continue

        # `data_type:` and friends only ever hang off a *column* entry. Seeing one
        # proves the `- name:` above it was a column, which is the only evidence
        # available when the hunk opens mid-list with no `columns:` line in context.
        if stripped.startswith(("data_type:", "quote:")) and not in_columns:
            in_columns = True
            columns_indent = model_indent - 1 if model_indent >= 0 else -1
            if current_model is not None:
                # That heading was a column all along, so retract it and replay it as
                # a column of the unknown enclosing model.
                retracted = result.pop(current_model, None)
                if retracted is not None:
                    orphan_added.update(retracted[0])
                    orphan_removed.update(retracted[1])
                if last_prefix == "+":
                    orphan_added.add(current_model.lower())
                elif last_prefix == "-":
                    orphan_removed.add(current_model.lower())
                current_model, model_indent = None, -1
            continue

        match = _YAML_COLUMN.match(line)
        if not match:
            if indent <= model_indent:
                in_columns = False
            continue

        name = match.group(1)

        if tests_indent >= 0:
            # `- name: order_id` under tests: is a test reference. Not a column.
            continue

        # A `- name:` is a model heading only when we can see it is outside a columns:
        # list. When the hunk opens *inside* one — no `columns:` line in context, which
        # is the common case for a mid-file edit — guessing a heading is what silently
        # dropped the node: the column names became dict keys, matched no model, and the
        # change vanished with no warning. Attributing nothing is recoverable; a
        # confident wrong attribution is not.
        # A `- name:` at or above the `columns:` indent has dedented out of the list,
        # so it is the next model heading, not another column.
        if in_columns and columns_indent >= 0 and indent <= columns_indent:
            in_columns = False

        if in_columns and indent > columns_indent:
            if current_model is None:
                # A column with no discoverable owner. Recorded, not guessed at.
                if prefix == "+":
                    orphan_added.add(name.lower())
                elif prefix == "-":
                    orphan_removed.add(name.lower())
                continue
            added, removed = result.setdefault(current_model, (set(), set()))
            if prefix == "+":
                added.add(name.lower())
            elif prefix == "-":
                removed.add(name.lower())
            continue

        if not in_columns:
            current_model, model_indent, last_prefix = name, indent, prefix
            result.setdefault(name, (set(), set()))

    return result, (orphan_added, orphan_removed)


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
        per_model, orphans = _scan_yaml_hunk(changed.hunk_lines) if is_yaml else ({}, (set(), set()))
        orphan_removed = orphans[1]
        # One node in the file means the orphans have exactly one possible owner, so
        # attribute them. More than one and attributing would flag a model that merely
        # shares the file — the day-2 false positive.
        if orphan_removed and len(nodes) == 1:
            per_model.setdefault(nodes[0].name, (set(), set()))[1].update(orphan_removed)
            orphan_removed = set()

        for node in nodes:
            if is_yaml:
                # Attribution narrows a shared schema.yml to the model actually edited,
                # but only when it found any model block at all. When it found none —
                # a hunk opening inside a columns: list, or an exposures.yml with no
                # columns: key — falling through to "no columns, still changed" keeps
                # the node visible. The previous membership gate dropped it entirely,
                # so a dropped contract column produced no output at all: not a wrong
                # severity, silence. Design rule 4.
                if per_model:
                    if node.name not in per_model:
                        continue
                    added, removed = per_model[node.name]
                else:
                    added, removed = set(), set()
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
                    unattributed_removed_columns=tuple(sorted(orphan_removed)),
                )
            )

    return resolved, unresolved
