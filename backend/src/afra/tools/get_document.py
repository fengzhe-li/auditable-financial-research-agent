"""get_document() - fetch a specific document's metadata by identifier.

Phase 2 correction: in Phase 1, get_document() actually did what
retrieve_section() now does (fetched section text and produced Evidence).
That conflated two different tool roles from docs/ARCHITECTURE.md#controlled-tool-layer
("get_document(): fetch a specific filing by identifier" vs. "retrieve_section():
fetch a named section from a document"). Phase 2 needs both roles
distinguished, since search_documents() now needs something to return
(document metadata, not evidence) - see the recorded deviation in
docs/ROADMAP.md.

get_document() is a pure lookup: no task_id, no Evidence, no persistence.
It does not touch the classification/provenance machinery that
retrieve_section() does, because it does not return document *content* -
only identity and available sections.
"""

from __future__ import annotations

from afra.tools.fixtures import FixtureDocument, get_fixture_document


def get_document(document_id: str) -> FixtureDocument:
    """Raises tools.fixtures.UnknownDocumentError for an unknown id."""
    return get_fixture_document(document_id)
