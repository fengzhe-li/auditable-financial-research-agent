"""retrieve_section() - fetch a named section of a document and return it as
Evidence.

This is the evidence-producing tool (was Phase 1's get_document() logic,
moved here under its correct name - see the module docstring in
tools/get_document.py for why). Per docs/ARCHITECTURE.md#controlled-tool-layer,
a tool call returns retrieved content as data, tagged with a classification,
for the caller to treat as untrusted evidence - never as instructions.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from afra.domain.models import Evidence
from afra.tools.fixtures import get_fixture_document


def retrieve_section(task_id: str, document_id: str, section: str) -> Evidence:
    """Raises tools.fixtures.UnknownDocumentError for an unknown document_id,
    or KeyError for an unknown section - callers (the orchestrator) turn
    that into a FAILED transition per docs/STATE_MACHINE.md's
    GATHERING_EVIDENCE -> FAILED row.
    """
    document = get_fixture_document(document_id)
    raw_text = document.sections[section]
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return Evidence(
        task_id=task_id,
        source_document_id=document.document_id,
        source_location=f"{document.document_id}#{section}",
        raw_text=raw_text,
        source_timestamp=datetime(
            document.published_at.year,
            document.published_at.month,
            document.published_at.day,
            tzinfo=timezone.utc,
        ),
        retrieval_tool="retrieve_section",
        content_hash=content_hash,
        # Phase 4: Evidence now inherits the source document's real
        # classification (see FixtureDocument.classification in
        # tools/fixtures.py) instead of always being hardcoded PUBLIC -
        # afra.policy.enforcement reads this value to decide model routing.
        classification=document.classification,
    )
