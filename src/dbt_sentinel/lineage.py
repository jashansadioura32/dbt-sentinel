"""Load a dbt manifest into a lineage graph and walk it.

Deliberately dependency-free: the manifest already ships `child_map`, so pulling in
networkx buys nothing but an install. Traversal here is BFS over an adjacency dict.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .models import BlastRadius, Node, NodeKind

# Test nodes are children of every model they cover. Including them makes blast radius
# meaningless (a model with 12 tests looks like it has 12 downstream consumers).
_EXCLUDED_KINDS = {NodeKind.TEST}

_SUPPORTED_MANIFEST_VERSIONS = range(7, 15)  # dbt 1.3 -> 1.10


class ManifestError(ValueError):
    pass


class Lineage:
    def __init__(
        self,
        nodes: dict[str, Node],
        child_map: dict[str, list[str]],
        generated_at: datetime | None = None,
        column_tests: dict[str, dict[str, tuple[str, ...]]] | None = None,
    ):
        self._nodes = nodes
        self._child_map = child_map
        self.generated_at = generated_at
        self._column_tests = column_tests or {}

    # ---------- construction ----------

    @classmethod
    def from_path(cls, manifest_path: str | Path) -> Lineage:
        path = Path(manifest_path)
        if not path.exists():
            raise ManifestError(
                f"manifest not found at {path}. Run `dbt parse` (fastest; runs no warehouse "
                f"queries) or `dbt compile` first, or pass --manifest."
            )
        with path.open(encoding="utf-8") as fh:
            return cls.from_manifest(json.load(fh))

    @classmethod
    def from_manifest(cls, manifest: dict) -> Lineage:
        cls._check_version(manifest)
        nodes = cls._parse_nodes(manifest)
        child_map = cls._child_map(manifest, nodes)
        return cls(nodes, child_map, _parse_generated_at(manifest), cls._parse_tests(manifest))

    def staleness_warning(self, compared_to: datetime | None = None) -> str | None:
        """Warn when the manifest predates the code being reviewed.

        A stale manifest shrinks every blast radius: models added since the compile are
        invisible, and reach is computed from an outdated graph. The tool still produces
        a confident-looking review, which is the dangerous part — so say so explicitly
        rather than letting the reader assume the lineage is current.
        """
        if self.generated_at is None:
            return (
                "Manifest has no `generated_at` timestamp, so its freshness cannot be "
                "checked. Blast radius may be computed from a stale graph."
            )
        if compared_to is None:
            return None
        if self.generated_at >= compared_to:
            return None
        age = compared_to - self.generated_at
        hours = age.total_seconds() / 3600
        return (
            f"Manifest was compiled {hours:.1f}h before the change under review "
            f"({self.generated_at.isoformat()}). Models added since are invisible and "
            f"blast radius is under-reported. Re-run `dbt parse` locally, or `dbt compile` on "
            f"the base branch in CI."
        )

    @staticmethod
    def _check_version(manifest: dict) -> None:
        raw = manifest.get("metadata", {}).get("dbt_schema_version", "")
        # e.g. https://schemas.getdbt.com/dbt/manifest/v12.json
        try:
            version = int(raw.rsplit("/v", 1)[-1].split(".")[0])
        except (ValueError, IndexError):
            return  # unknown shape; parse optimistically rather than hard-fail
        if version not in _SUPPORTED_MANIFEST_VERSIONS:
            raise ManifestError(
                f"manifest schema v{version} is outside the tested range "
                f"v{_SUPPORTED_MANIFEST_VERSIONS.start}-v{_SUPPORTED_MANIFEST_VERSIONS.stop - 1}"
            )

    @staticmethod
    def _parse_nodes(manifest: dict) -> dict[str, Node]:
        nodes: dict[str, Node] = {}

        for unique_id, raw in (manifest.get("nodes") or {}).items():
            # Disabled nodes live in a separate `disabled` key, so anything here is live.
            kind = _kind_from_id(unique_id)
            if kind in _EXCLUDED_KINDS:
                continue
            config = raw.get("config") or {}
            contract = raw.get("contract") or {}
            nodes[unique_id] = Node(
                unique_id=unique_id,
                name=raw.get("name", ""),
                kind=kind,
                path=raw.get("original_file_path"),
                patch_path=_strip_package(raw.get("patch_path")),
                materialization=config.get("materialized"),
                is_contracted=bool(contract.get("enforced")),
                access=raw.get("access"),
                depends_on=tuple((raw.get("depends_on") or {}).get("nodes", [])),
                columns=tuple((raw.get("columns") or {}).keys()),
                raw_code=raw.get("raw_code") or raw.get("raw_sql"),
                column_types=_declared_types(raw),
            )

        for unique_id, raw in (manifest.get("sources") or {}).items():
            nodes[unique_id] = Node(
                unique_id=unique_id,
                name=raw.get("name", ""),
                kind=NodeKind.SOURCE,
                path=raw.get("original_file_path"),
                columns=tuple((raw.get("columns") or {}).keys()),
                column_types=_declared_types(raw),
            )

        for unique_id, raw in (manifest.get("exposures") or {}).items():
            owner = (raw.get("owner") or {}).get("name") or (raw.get("owner") or {}).get("email")
            nodes[unique_id] = Node(
                unique_id=unique_id,
                name=raw.get("name", ""),
                kind=NodeKind.EXPOSURE,
                path=raw.get("original_file_path"),
                depends_on=tuple((raw.get("depends_on") or {}).get("nodes", [])),
                owner=owner,
            )

        return nodes

    @staticmethod
    def _parse_tests(manifest: dict) -> dict[str, dict[str, tuple[str, ...]]]:
        """Which tests cover which column, keyed by the node they're attached to.

        Test nodes are excluded from the graph (see `_EXCLUDED_KINDS`), but what they
        assert is exactly the evidence the agent needs to judge a join: a key with a
        `unique` test can't fan a join out, and one without it might. Answering that from
        the manifest keeps it a lookup rather than a guess (design rule 1).

        Model-level tests (`unique_combination_of_columns`) are filed under the empty
        column name, with their columns, since a composite key is unique only as a set.
        """
        tests: dict[str, dict[str, list[str]]] = {}
        for unique_id, raw in (manifest.get("nodes") or {}).items():
            if _kind_from_id(unique_id) is not NodeKind.TEST:
                continue
            metadata = raw.get("test_metadata") or {}
            name = metadata.get("name")
            if not name:
                continue  # a singular test: arbitrary SQL, asserts nothing nameable
            if metadata.get("namespace"):
                name = f"{metadata['namespace']}.{name}"
            attached = raw.get("attached_node")
            if not attached:
                # Pre-1.5 manifests have no attached_node. Only a test on exactly one
                # node is attributable; a relationships test depends on two.
                depends = (raw.get("depends_on") or {}).get("nodes") or []
                if len(depends) != 1:
                    continue
                attached = depends[0]
            column = raw.get("column_name") or ""
            if not column and (combo := (metadata.get("kwargs") or {}).get("combination_of_columns")):
                name = f"{name}({', '.join(combo)})"
            tests.setdefault(attached, {}).setdefault(column, []).append(name)
        return {
            node: {column: tuple(sorted(names)) for column, names in by_column.items()}
            for node, by_column in tests.items()
        }

    def tests_for(self, unique_id: str) -> dict[str, tuple[str, ...]]:
        """Test names per column for one node. `""` holds model-level tests."""
        return self._column_tests.get(unique_id, {})

    @staticmethod
    def _child_map(manifest: dict, nodes: dict[str, Node]) -> dict[str, list[str]]:
        """Prefer the manifest's own child_map; rebuild from depends_on if absent."""
        raw_map = manifest.get("child_map")
        if raw_map:
            return {
                parent: [c for c in children if c in nodes]
                for parent, children in raw_map.items()
                if parent in nodes
            }

        built: dict[str, list[str]] = {uid: [] for uid in nodes}
        for uid, node in nodes.items():
            for parent in node.depends_on:
                if parent in built:
                    built[parent].append(uid)
        return built

    # ---------- access ----------

    def get(self, unique_id: str) -> Node | None:
        return self._nodes.get(unique_id)

    def all_nodes(self) -> list[Node]:
        """Every node in the graph. For callers that need a name or path index."""
        return list(self._nodes.values())

    def nodes_by_file_path(self, file_path: str) -> list[Node]:
        """Map a repo-relative file path to every node it defines or documents.

        Two subtleties the manifest forces on us:
        1. Manifest paths are relative to the dbt project root, which may be a
           subdirectory of the repo, so match on suffix rather than equality.
        2. A schema.yml is the `patch_path` of many nodes, not the `original_file_path`
           of one. Editing it can affect every model it documents.
        """
        normalised = file_path.replace("\\", "/").lstrip("./")
        matches = [
            node
            for node in self._nodes.values()
            if _path_matches(normalised, node.path) or _path_matches(normalised, node.patch_path)
        ]
        # Definition files sort first so callers that want a single node get the right one.
        matches.sort(key=lambda n: (not _path_matches(normalised, n.path), n.name))
        return matches

    def by_file_path(self, file_path: str) -> Node | None:
        matches = self.nodes_by_file_path(file_path)
        return matches[0] if matches else None

    def children(self, unique_id: str) -> list[Node]:
        return [self._nodes[c] for c in self._child_map.get(unique_id, []) if c in self._nodes]

    # ---------- traversal ----------

    def blast_radius(self, unique_id: str, max_depth: int | None = None) -> BlastRadius:
        """BFS over children. Cycle-safe via `seen`; dbt forbids cycles but manifests
        from partial parses have been known to contain them."""
        root = self._nodes.get(unique_id)
        if root is None:
            raise KeyError(f"{unique_id} not present in manifest")

        seen: set[str] = {unique_id}
        depth_by_id: dict[str, int] = {}
        downstream: list[Node] = []
        queue: deque[tuple[str, int]] = deque([(unique_id, 0)])

        while queue:
            current, depth = queue.popleft()
            if max_depth is not None and depth >= max_depth:
                continue
            for child_id in self._child_map.get(current, []):
                if child_id in seen or child_id not in self._nodes:
                    continue
                seen.add(child_id)
                depth_by_id[child_id] = depth + 1
                downstream.append(self._nodes[child_id])
                queue.append((child_id, depth + 1))

        downstream.sort(key=lambda n: (depth_by_id[n.unique_id], n.name))
        return BlastRadius(root=root, downstream=downstream, depth_by_id=depth_by_id)

    def __len__(self) -> int:
        return len(self._nodes)


def _kind_from_id(unique_id: str) -> NodeKind:
    prefix = unique_id.split(".", 1)[0]
    try:
        return NodeKind(prefix)
    except ValueError:
        return NodeKind.OTHER


def _strip_package(patch_path: str | None) -> str | None:
    """`jaffle_shop://models/staging/schema.yml` -> `models/staging/schema.yml`"""
    if not patch_path:
        return None
    return patch_path.split("://", 1)[-1]


def _declared_types(raw: dict) -> tuple[tuple[str, str], ...]:
    """(column, data_type) for columns that declare one. Undeclared is not "unknown
    type": it's no claim at all, and the agent must not read it as one."""
    return tuple(
        (name, str(column["data_type"]))
        for name, column in (raw.get("columns") or {}).items()
        if (column or {}).get("data_type")
    )


def _parse_generated_at(manifest: dict) -> datetime | None:
    """dbt writes `metadata.generated_at` as UTC ISO-8601, usually with a trailing Z."""
    raw = (manifest.get("metadata") or {}).get("generated_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive timestamps are UTC by dbt's convention; tag them so comparisons never raise.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _posix(path: str | None) -> str | None:
    """Normalise separators before any path comparison.

    dbt writes `original_file_path` and `patch_path` using the separator of the OS that
    ran `dbt compile`, so a manifest compiled on Windows carries `models\\staging\\x.sql`.
    Git diffs always use forward slashes. Comparing the two raw means every file fails to
    resolve on Windows and the tool reports nothing on a real PR — the worst failure mode
    available, since it looks like a clean review.
    """
    if not path:
        return None
    return path.replace("\\", "/")


def _path_matches(candidate: str, manifest_path: str | None) -> bool:
    normalised = _posix(manifest_path)
    if not normalised:
        return False
    return candidate.endswith(normalised) or normalised.endswith(candidate)
