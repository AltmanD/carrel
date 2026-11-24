"""Graph value objects shared by the read-only builders and the JSON API.

The browser owns layout; this module only defines the topology and the display
fields that the server can compute cheaply and deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Node roles. ``paper`` is always a paper in this library; ``external`` is a work
# only known through a relation and never links to a reading page.
ROLES = ("category", "paper", "tag", "external")

# Edge roles. ``membership`` and ``tag`` are organisation, ``cites`` and
# ``similar`` come from the relation providers.
EDGE_ROLES = ("membership", "tag", "cites", "similar", "motivation")


@dataclass(slots=True)
class Node:
    id: str
    role: str
    label: str
    title: str = ""
    parent: str = ""
    url: str = ""
    link_kind: str = "unavailable"
    sort_key: str = ""
    focus: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "label": self.label,
            "title": self.title or self.label,
            "parent": self.parent,
            "url": self.url,
            "link_kind": self.link_kind,
            "sort_key": self.sort_key,
            "focus": self.focus,
            "meta": self.meta,
        }


@dataclass(slots=True)
class Edge:
    source: str
    target: str
    role: str
    label: str = ""
    evidence: str = ""
    primary: bool = True
    basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "role": self.role,
            "label": self.label,
            "evidence": self.evidence,
            "primary": self.primary,
            "basis": self.basis,
        }


@dataclass(slots=True)
class Graph:
    scope: str
    focus: str = ""
    revision: str = ""
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": self.scope,
            "focus": self.focus,
            "revision": self.revision,
            "stats": self.stats,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "notes": self.notes,
        }
