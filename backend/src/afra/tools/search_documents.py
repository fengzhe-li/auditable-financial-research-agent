"""search_documents() - find candidate documents by company and/or keyword.

Returns document metadata only (FixtureDocument), never Evidence or raw
section text - a search result is a set of candidates to consider, not
retrieved content. Actually retrieving a section's text (and thereby
creating Evidence, with provenance and classification) is
retrieve_section()'s job. This mirrors docs/ARCHITECTURE.md's
search_filings() role, renamed search_documents() for this phase - see the
recorded deviation in docs/ROADMAP.md.
"""

from __future__ import annotations

from afra.tools.fixtures import FixtureDocument, search_fixture_documents


def search_documents(company: str | None = None, keyword: str | None = None) -> list[FixtureDocument]:
    return search_fixture_documents(company=company, keyword=keyword)
