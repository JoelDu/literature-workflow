"""Citation-graph domain constants shared by writers, readers, and audits."""
from enum import Enum


class CitationEdgeStatus(str, Enum):
    """Persisted lifecycle values for a citation edge."""

    CONFIRMED = "CONFIRMED"
    CANDIDATE = "CANDIDATE"
    UNRESOLVED_EXTERNAL = "UNRESOLVED_EXTERNAL"
    DISPUTED = "DISPUTED"
    REVOKED = "REVOKED"

