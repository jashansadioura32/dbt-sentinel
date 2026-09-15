"""Core domain types. Everything downstream depends on these, nothing else."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NodeKind(str, Enum):
    MODEL = "model"
    SEED = "seed"
    SNAPSHOT = "snapshot"
    SOURCE = "source"
    EXPOSURE = "exposure"
    TEST = "test"
    OTHER = "other"


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass(frozen=True)
class Node:
    """A node in the dbt DAG, normalised across the manifest's many shapes."""

    unique_id: str
    name: str
    kind: NodeKind
    path: str | None = None
    patch_path: str | None = None  # the schema.yml that documents this node
    materialization: str | None = None
    is_contracted: bool = False
    access: str | None = None  # public | protected | private
    depends_on: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    owner: str | None = None  # exposures carry this; useful for review routing

    @property
    def is_consumer_facing(self) -> bool:
        """Exposures and public models are where breakage becomes someone else's problem."""
        return self.kind is NodeKind.EXPOSURE or self.access == "public"


@dataclass(frozen=True)
class ChangedFile:
    path: str
    change_type: ChangeType
    added_lines: tuple[str, ...] = ()
    removed_lines: tuple[str, ...] = ()
    previous_path: str | None = None
    hunk_lines: tuple[str, ...] = ()  # prefixed lines, context included


@dataclass
class ChangedNode:
    """A dbt node the PR touches, joined back to its manifest entry."""

    node: Node
    change_type: ChangeType
    file: ChangedFile
    added_columns: tuple[str, ...] = ()
    removed_columns: tuple[str, ...] = ()
    has_semantic_change: bool = True

    @property
    def is_structural(self) -> bool:
        """Does this change alter the contract other models rely on?"""
        return bool(
            self.added_columns
            or self.removed_columns
            or self.change_type in (ChangeType.DELETED, ChangeType.RENAMED)
        )


@dataclass
class BlastRadius:
    """Everything downstream of a changed node, with the reachable consumer surface."""

    root: Node
    downstream: list[Node] = field(default_factory=list)
    depth_by_id: dict[str, int] = field(default_factory=dict)

    @property
    def exposures(self) -> list[Node]:
        return [n for n in self.downstream if n.kind is NodeKind.EXPOSURE]

    @property
    def contracted(self) -> list[Node]:
        return [n for n in self.downstream if n.is_contracted]

    @property
    def public(self) -> list[Node]:
        return [n for n in self.downstream if n.access == "public"]

    @property
    def size(self) -> int:
        return len(self.downstream)
