"""Read-only knowledge graph for the Carrel web UI."""

from __future__ import annotations

from carrel.graph.build import (  # noqa: F401
    DEFAULT_SINGLE_MAX_PAPERS,
    SINGLE_MAX_OPTIONS,
    build_library_graph,
    build_paper_graph,
    revision_of,
    short_label,
)
from carrel.graph.links import relation_link  # noqa: F401
from carrel.graph.model import Edge, Graph, Node  # noqa: F401
