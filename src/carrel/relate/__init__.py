"""Relation package."""

from carrel.relate.link import RelationBundle, RelationCollector, link_to_library
from carrel.relate.openalex import OpenAlexClient, OpenAlexError
from carrel.relate.sciverse import SciverseClient, SciverseError

__all__ = [
    "OpenAlexClient",
    "OpenAlexError",
    "RelationBundle",
    "RelationCollector",
    "SciverseClient",
    "SciverseError",
    "link_to_library",
]
