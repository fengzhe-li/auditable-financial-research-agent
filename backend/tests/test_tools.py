"""Direct tests of the three controlled tools, independent of the
orchestrator: search_documents(), get_document(), retrieve_section().
"""

from __future__ import annotations

import pytest

from afra.domain.enums import Classification
from afra.tools.fixtures import UnknownDocumentError
from afra.tools.get_document import get_document
from afra.tools.retrieve_section import retrieve_section
from afra.tools.search_documents import search_documents


def test_search_documents_by_company_returns_both_filing_years():
    """Contoso now has non-10-K fixture documents too (Phase 4's
    INTERNAL/CONFIDENTIAL/RESTRICTED additions - see tools/fixtures.py), so
    this checks the two filing years are present rather than asserting they
    are the *only* Contoso documents.
    """
    results = search_documents(company="Contoso")
    ids = {d.document_id for d in results}
    assert {"CONTOSO-2024-10K", "CONTOSO-2025-10K"} <= ids


def test_search_documents_by_keyword_matches_across_companies():
    results = search_documents(keyword="AI infrastructure")
    companies = {d.company for d in results}
    assert "Contoso Cloud Corp" in companies
    assert "Fabrikam Systems Inc" in companies


def test_search_documents_with_no_match_returns_empty():
    results = search_documents(company="Northwind Traders")
    assert results == []


def test_search_documents_returns_metadata_not_evidence():
    """A search result is document metadata, not retrieved content: it has
    no flat raw_text field (retrieve_section() is what actually returns
    text, as Evidence). Phase 4 deliberately gives FixtureDocument a
    `classification` field - document classification is assigned at
    ingestion (docs/DATA_CLASSIFICATION.md) and must be visible from
    metadata alone, before anything is retrieved, so a caller (or policy
    layer) can reason about a document without first pulling its content.
    """
    results = search_documents(company="Contoso")
    for doc in results:
        assert not hasattr(doc, "raw_text")
        assert isinstance(doc.classification, Classification)


def test_get_document_returns_metadata_for_known_id():
    doc = get_document("CONTOSO-2025-10K")
    assert doc.company == "Contoso Cloud Corp"
    assert "risk_factors" in doc.sections


def test_get_document_raises_for_unknown_id():
    with pytest.raises(UnknownDocumentError):
        get_document("NORTHWIND-2025-10K")


def test_retrieve_section_returns_evidence_with_full_provenance():
    evidence = retrieve_section("task-1", "CONTOSO-2025-10K", "risk_factors")
    assert evidence.task_id == "task-1"
    assert evidence.source_document_id == "CONTOSO-2025-10K"
    assert evidence.source_location == "CONTOSO-2025-10K#risk_factors"
    assert evidence.source_timestamp is not None
    assert evidence.retrieved_at is not None
    assert evidence.content_hash
    assert evidence.classification == Classification.PUBLIC
    assert "capital expenditure" in evidence.raw_text


def test_retrieve_section_raises_for_unknown_document():
    with pytest.raises(UnknownDocumentError):
        retrieve_section("task-1", "NORTHWIND-2025-10K", "risk_factors")


def test_retrieve_section_raises_for_unknown_section():
    with pytest.raises(KeyError):
        retrieve_section("task-1", "CONTOSO-2025-10K", "executive_compensation")


def test_retrieve_section_content_hash_is_deterministic():
    e1 = retrieve_section("task-1", "CONTOSO-2025-10K", "risk_factors")
    e2 = retrieve_section("task-2", "CONTOSO-2025-10K", "risk_factors")
    assert e1.content_hash == e2.content_hash
