"""Load a dbt manifest into a lineage graph and walk it.

Deliberately dependency-free: the manifest already ships `child_map`, so pulling in
networkx buys nothing but an install. Traversal here is BFS over an adjacency dict.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from .models import BlastRadius, Node, NodeKind

# Test nodes are children of every model they cover. Including them makes blast radius
# meaningless (a model with 12 tests looks like it has 12 downstream consumers).
_EXCLUDED_KINDS = {NodeKind.TEST}

_SUPPORTED_MANIFEST_VERSIONS = range(7, 15)  # dbt 1.3 -> 1.10


class ManifestError(ValueError):
    pass


class Lineage:
    def __init__(self, nodes: dict[str, Node], child_map: dict[str, list[str]]):
        self._nodes = nodes
        self._child_map = child_map

    # ---------- construction ----------

    @classmethod
    def from_path(cls, manifest_path: str | Path) -> Lineage:
        path = Path(manifest_path)
        if not path.exists():
            raise ManifestError(
                f"manifest not found at {path}. Run `dbt compile` or `dbt docs generate` first."
            )
        with path.open(encoding="utf-8") as fh:
            return cls.from_manifest(json.load(fh))

    @classmethod
    def from_manifest(cls, manifest: dict) -> Lineage:
        cls._check_version(manifest)
        nodes = cls._parse_nodes(manifest)
        child_map = cls._child_map(manifest, nodes)
        return cls(nodes, child_map)

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
            )

        for unique_id, raw in (manifest.get("sources") or {}).items():
            nodes[unique_id] = Node(
                unique_id=unique_id,
                name=raw.get("name", ""),
                kind=NodeKind.SOURCE,
                path=raw.get("original_file_path"),
                columns=tuple((raw.get("columns") or {}).keys()),
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


def _path_matches(candidate: str, manifest_path: str | None) -> bool:
    if not manifest_path:
        return False
    return candidate.endswith(manifest_path) or manifest_path.endswith(candidate)
