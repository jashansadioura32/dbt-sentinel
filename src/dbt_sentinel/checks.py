"""Deterministic per-file checks: a diff in, structured findings out.

The spec is [docs/CHECKS.md](../../docs/CHECKS.md) and it was written before this module
existed. Read it before adding a check; in particular the "Does NOT fire when" column,
which is the false-positive contract each check has to meet.

Two structural decisions carry this layer, and both are load-bearing rather than stylistic.

**Checks never enter the blast radius.** A lint finding has no reach — `select *` in a
model with 200 consumers is exactly as bad as in a leaf — so folding one into
`Assessment.severity` would either amplify it by reach, which is false and inflates the
false-positive rate, or leave it on a scale whose entire meaning is *structural trigger x
reach* while having neither. Checks render in their own section and no check may return
`high`, which is what keeps them out of the commit status without inventing a second
severity axis.

**Checks read only added lines, never whole files.** A whole-file check fires on every
pre-existing violation in every unrelated PR. That is unusable on a large repo, which is
why reviewers that scan whole files end up bolting on a frozen list of grandfathered
violations. Scoping to added lines means pre-existing violations never fire and none of
that machinery is needed. The honest cost: a check cannot see that an *existing* line is
wrong, only that a *new* one is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Callable

from .lineage import Lineage
from .models import ChangedFile, ChangeType, Node, NodeKind

# Severity vocabulary matches report.assess, minus "high" — see the module docstring.
_ALLOWED_SEVERITIES = ("low", "medium")


@dataclass(frozen=True)
class CheckFinding:
    """One violation, attributable to a file and a check."""

    check_id: str
    title: str
    severity: str  # "low" | "medium" — never "high"
    path: str
    message: str
    suggestion: str
    node_name: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in _ALLOWED_SEVERITIES:
            raise ValueError(
                f"check {self.check_id!r} returned severity {self.severity!r}; "
                f"checks are capped at {_ALLOWED_SEVERITIES} because HIGH means "
                f"'a consumer breaks on merge', which no lint finding establishes"
            )


@dataclass(frozen=True)
class CheckContext:
    """Everything a check may read, beyond the file it is looking at.

    `models_documented_in_diff` exists because a model added in this PR is absent from the
    base branch's manifest, and so is the schema.yml entry added alongside it. Neither is
    resolvable through lineage, so the diff itself is the only place to look.
    """

    lineage: Lineage
    models_documented_in_diff: frozenset[str]


Check = Callable[[ChangedFile, Node | None, CheckContext], list[CheckFinding]]


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    fn: Check
    path_globs: tuple[str, ...]
    category: str  # "structure" | "testing" | "portability" | "correctness"


def _added(file: ChangedFile) -> tuple[str, ...]:
    """Added lines with their trailing newline stripped. The only content a check sees."""
    return tuple(line.rstrip("\n") for line in file.added_lines)


def _is_sql_comment(line: str) -> bool:
    return line.lstrip().startswith("--")


# ---------------------------------------------------------------------------
# missing-schema-entry
# ---------------------------------------------------------------------------

def check_missing_schema_entry(
    file: ChangedFile, node: Node | None, ctx: CheckContext
) -> list[CheckFinding]:
    """A new model with no schema.yml entry enters the project untested and undescribed.

    Scoped to ADDED files only. A pre-existing model without a patch_path is someone
    else's debt, and flagging it on every PR that happens to touch the file is how a
    reviewer gets muted.

    The node is almost always None here, and that is not a failure: a model added in this
    PR does not exist in the base branch's manifest, which is the graph we resolve
    against. So the model name comes from the filename and the schema entry is looked for
    in the diff itself — the only place it could be, since a YAML edit in this PR is also
    absent from the base manifest. Requiring a resolved node would have made the check
    unable to fire on the exact case it exists for.
    """
    if file.change_type is not ChangeType.ADDED:
        return []
    if not file.path.endswith(".sql"):
        return []
    if node is not None and (node.kind is not NodeKind.MODEL or node.patch_path):
        return []

    model_name = file.path.replace("\\", "/").rsplit("/", 1)[-1][: -len(".sql")]
    if model_name in ctx.models_documented_in_diff:
        return []

    return [CheckFinding(
        check_id="missing-schema-entry",
        title="New model has no schema.yml entry",
        severity="medium",
        path=file.path,
        node_name=model_name,
        message=(
            f"`{model_name}` is added in this PR with no entry in any schema.yml, so it "
            f"has no description and no tests can be attached to it."
        ),
        suggestion=(
            f"Add a `- name: {model_name}` block to the schema.yml beside it, with a "
            f"description and at least a `unique`/`not_null` pair on its key."
        ),
    )]


# ---------------------------------------------------------------------------
# deprecated-tests-key
# ---------------------------------------------------------------------------

_TESTS_KEY = re.compile(r"^\s*tests:\s*$")


def check_deprecated_tests_key(
    file: ChangedFile, node: Node | None, ctx: CheckContext
) -> list[CheckFinding]:
    """`tests:` was renamed `data_tests:` in dbt 1.8. Purely factual, hence severity low.

    Matched with an anchored regex rather than a substring: `data_tests:` ends in the same
    eight characters, so `"tests:" in line` flags the correct spelling as the deprecated
    one — the near-miss fixture c02n exists to catch exactly that.
    """
    if not file.path.endswith((".yml", ".yaml")):
        return []
    # dbt_project.yml has its own `tests:` config key, which was not renamed.
    if file.path.endswith("dbt_project.yml"):
        return []

    if not any(_TESTS_KEY.match(line) for line in _added(file)):
        return []

    return [CheckFinding(
        check_id="deprecated-tests-key",
        title="Deprecated `tests:` key",
        severity="low",
        path=file.path,
        node_name=node.name if node else None,
        message=(
            "This PR adds a `tests:` block. dbt renamed the key to `data_tests:` in 1.8; "
            "`tests:` still works but is deprecated and will be removed."
        ),
        suggestion="Rename the key to `data_tests:`.",
    )]


# ---------------------------------------------------------------------------
# hardcoded-relation
# ---------------------------------------------------------------------------

# `from`/`join` followed by a dotted identifier: schema.table or database.schema.table.
# Requires at least one dot, so a bare CTE name never matches.
_HARDCODED = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)",
    re.IGNORECASE,
)


def check_hardcoded_relation(
    file: ChangedFile, node: Node | None, ctx: CheckContext
) -> list[CheckFinding]:
    """A hardcoded `schema.table` is invisible to dbt, so it defeats lineage itself.

    The dependency is missing from the DAG, the model is not rebuilt when its upstream
    changes, and a production database name is baked into SQL meant to run in dev too.
    """
    if not file.path.endswith(".sql"):
        return []

    findings: list[CheckFinding] = []
    seen: set[str] = set()
    for line in _added(file):
        if _is_sql_comment(line):
            continue
        # Strip Jinja before matching: `{{ source('finance', 'rates') }}` renders to a
        # dotted relation, but writing it that way is the correct form, not a violation.
        without_jinja = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", line)
        for match in _HARDCODED.finditer(without_jinja):
            relation = match.group(1)
            if relation.lower() in seen:
                continue
            seen.add(relation.lower())
            findings.append(CheckFinding(
                check_id="hardcoded-relation",
                title="Hardcoded relation instead of ref() or source()",
                severity="medium",
                path=file.path,
                node_name=node.name if node else None,
                message=(
                    f"`{relation}` is referenced directly, so dbt cannot see the "
                    f"dependency: it is missing from the DAG, this model will not rebuild "
                    f"when it changes, and the environment is hardcoded."
                ),
                suggestion=(
                    f"Replace `{relation}` with `{{{{ source(...) }}}}` if it is raw data, "
                    f"or `{{{{ ref(...) }}}}` if another dbt model builds it."
                ),
            ))
    return findings


# ---------------------------------------------------------------------------
# cross-layer-reference
# ---------------------------------------------------------------------------

# Layer ordering, lowest first. A model may reference its own layer or a lower one;
# referencing a higher layer inverts the DAG's intended direction.
#
# These prefixes are a convention, not a law — a project that lays its models out
# differently simply never matches, and the check stays silent rather than guessing.
_LAYERS: tuple[tuple[str, int], ...] = (
    ("models/staging/", 0),
    ("models/intermediate/", 1),
    ("models/marts/", 2),
)

_REF = re.compile(r"\{\{\s*ref\s*\(\s*['\"]([\w.]+)['\"]", re.IGNORECASE)


def _layer_of(path: str | None) -> int | None:
    if not path:
        return None
    normalised = path.replace("\\", "/")
    for prefix, rank in _LAYERS:
        if prefix in normalised:
            return rank
    return None


def check_cross_layer_reference(
    file: ChangedFile, node: Node | None, ctx: CheckContext
) -> list[CheckFinding]:
    """A staging model referencing a mart inverts the layering.

    Resolved through the manifest rather than by regex alone: the ref() name gives the
    target, and the target's own path decides its layer. A project whose paths do not
    match the known prefixes produces no finding.
    """
    if not file.path.endswith(".sql"):
        return []

    own_layer = _layer_of(file.path)
    if own_layer is None:
        return []

    by_name = {n.name: n for n in ctx.lineage.all_nodes() if n.kind is NodeKind.MODEL}

    findings: list[CheckFinding] = []
    seen: set[str] = set()
    for line in _added(file):
        if _is_sql_comment(line):
            continue
        for match in _REF.finditer(line):
            target_name = match.group(1)
            target = by_name.get(target_name)
            if target is None or target_name in seen:
                continue
            target_layer = _layer_of(target.path)
            if target_layer is None or target_layer <= own_layer:
                continue
            seen.add(target_name)
            findings.append(CheckFinding(
                check_id="cross-layer-reference",
                title="Reference points against the layering",
                severity="medium",
                path=file.path,
                node_name=node.name if node else None,
                message=(
                    f"This model references `{target_name}`, which sits in a later layer. "
                    f"Staging is meant to be the thin, source-shaped base everything else "
                    f"builds on, so pointing it downstream means a change to "
                    f"`{target_name}` now silently alters it."
                ),
                suggestion=(
                    f"Move the logic that needs `{target_name}` into a model at or above "
                    f"its layer, or read the same upstream source `{target_name}` reads."
                ),
            ))
    return findings


# ---------------------------------------------------------------------------
# null-comparison
# ---------------------------------------------------------------------------

# `!=` and `<>` first, then a bare `=` not preceded by another comparison character, so
# `>= null` is not misread as `= null` and `!=` is not matched twice.
_NULL_COMPARISON = re.compile(r"(!=|<>|(?<![<>!=])=)\s*null\b", re.IGNORECASE)
_ASSIGNMENT = re.compile(r"\bset\b", re.IGNORECASE)


def check_null_comparison(
    file: ChangedFile, node: Node | None, ctx: CheckContext
) -> list[CheckFinding]:
    """`x = null` is never true in SQL, not even when x is null.

    The filter silently matches no rows, and the model still builds, so the only symptom
    is a row count that's quietly too low. This needs no judgment, which is why it's a
    check rather than part of the agent's `null-handling` policy.
    """
    if not file.path.endswith(".sql"):
        return []

    for line in _added(file):
        if _is_sql_comment(line):
            continue
        # Inline comments and Jinja go first: `-- never use = null` is advice, and
        # `{% if x != none %}` is Jinja, where comparing with none is correct.
        code = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", line.split("--", 1)[0])
        match = _NULL_COMPARISON.search(code)
        # `update ... set col = null` assigns rather than compares, and is correct.
        if match is None or _ASSIGNMENT.search(code[: match.start()]):
            continue
        operator = match.group(1)
        fixed = "is not null" if operator in ("!=", "<>") else "is null"
        return [CheckFinding(
            check_id="null-comparison",
            title="Comparison with NULL using an operator",
            severity="medium",
            path=file.path,
            node_name=node.name if node else None,
            message=(
                f"`{match.group(0).strip()}` is never true in SQL, for any value, "
                f"including null. The condition silently matches no rows."
            ),
            suggestion=f"Write `{fixed}` instead of `{match.group(0).strip()}`.",
        )]
    return []


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec("missing-schema-entry", check_missing_schema_entry, ("models/**",), "structure"),
    CheckSpec("deprecated-tests-key", check_deprecated_tests_key, ("models/**",), "testing"),
    CheckSpec("hardcoded-relation", check_hardcoded_relation, ("models/**",), "portability"),
    CheckSpec("cross-layer-reference", check_cross_layer_reference, ("models/**",), "structure"),
    CheckSpec("null-comparison", check_null_comparison, ("models/**",), "correctness"),
)


_YAML_MODEL_NAME = re.compile(r"^\s*-\s*name:\s*['\"]?([\w.]+)['\"]?\s*$")


def _models_documented_in(files: list[ChangedFile]) -> frozenset[str]:
    """Model names given a `- name:` entry by a YAML added anywhere in this diff.

    Deliberately loose: a `- name:` under `columns:` is a column, not a model, and this
    does not distinguish them. The consequence of over-collecting is a missed finding,
    never a false one — and for a check whose whole job is to nag about a missing YAML
    entry, staying quiet when a YAML edit is present is the right way to be wrong.
    """
    documented: set[str] = set()
    for file in files:
        if not file.path.endswith((".yml", ".yaml")):
            continue
        for line in _added(file):
            if match := _YAML_MODEL_NAME.match(line):
                documented.add(match.group(1))
    return frozenset(documented)


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    normalised = path.replace("\\", "/")
    return any(fnmatch(normalised, glob) for glob in globs)


def run_checks(
    files: list[ChangedFile],
    changes: list,
    lineage: Lineage,
) -> list[CheckFinding]:
    """Run every applicable check over every changed file.

    Takes `files` rather than only the resolved `changes` on purpose: two checks are about
    the *absence* of something — a model with no schema entry, a file dbt cannot see — and
    a file that failed to resolve to a manifest node is exactly where silence is most
    dangerous (design rule 4). The resolved nodes are threaded through so a check can name
    the model when one is known.
    """
    node_by_path = {c.file.path: c.node for c in changes}
    ctx = CheckContext(
        lineage=lineage,
        models_documented_in_diff=_models_documented_in(files),
    )

    findings: list[CheckFinding] = []
    for file in files:
        if not _matches(file.path, ("models/**",)):
            continue
        node = node_by_path.get(file.path)
        for spec in CHECKS:
            if not _matches(file.path, spec.path_globs):
                continue
            findings.extend(spec.fn(file, node, ctx))

    # Stable ordering so the rendered comment does not churn between identical runs.
    rank = {"medium": 0, "low": 1}
    return sorted(findings, key=lambda f: (rank.get(f.severity, 9), f.path, f.check_id))
