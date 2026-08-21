"""litreview — 自动化文献综述分析与检索增强工作站。"""

__version__ = "0.2.0"

from litreview.library_store import (
    get_dashboard_stratified_stats,
    list_library_documents,
    get_document_full_detail,
    list_discovery_candidates_paginated
)
from litreview.citation_network import (
    query_citation_network,
    get_full_citation_graph,
    get_seed_subgraph
)
from litreview.ingestion_engine import (
    DocumentIngestionEngine,
    IngestionStrategy,
    OpenAccessCandidateStrategy,
    verify_document_file,
    VerificationResult,
    IngestionResult
)
