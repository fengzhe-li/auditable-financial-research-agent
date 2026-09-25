"""Direct tests of the router's decision functions, independent of the
orchestrator.
"""

from __future__ import annotations

from datetime import date

from afra.routing.router import decide_target_companies, pick_latest_document, section_for_axis
from afra.tools.fixtures import FixtureDocument

KNOWN_COMPANIES = ["Contoso Cloud Corp", "Fabrikam Systems Inc"]


def test_subquestion_naming_one_company_routes_to_that_company_only():
    companies, rationale = decide_target_companies(
        "What does Contoso Cloud Corp disclose about AI infrastructure investment risk?",
        KNOWN_COMPANIES,
    )
    assert companies == ["Contoso Cloud Corp"]
    assert "Contoso Cloud Corp" in rationale


def test_subquestion_naming_both_companies_routes_to_both():
    companies, rationale = decide_target_companies(
        "How does Contoso Cloud Corp's disclosure compare to Fabrikam Systems Inc's?",
        KNOWN_COMPANIES,
    )
    assert set(companies) == {"Contoso Cloud Corp", "Fabrikam Systems Inc"}


def test_subquestion_naming_no_company_falls_back_to_all_known_companies():
    companies, rationale = decide_target_companies(
        "How do the two disclosures differ?", KNOWN_COMPANIES
    )
    assert companies == KNOWN_COMPANIES
    assert "does not name a specific company" in rationale


def test_section_for_axis_maps_known_axes_to_risk_factors():
    assert section_for_axis("risk") == "risk_factors"
    assert section_for_axis("capital expenditure") == "risk_factors"


def test_section_for_axis_defaults_for_unknown_or_missing_axis():
    assert section_for_axis(None) == "risk_factors"
    assert section_for_axis("some unknown axis") == "risk_factors"


def _doc(document_id: str, year: int) -> FixtureDocument:
    return FixtureDocument(
        document_id=document_id, company="Contoso Cloud Corp", title="t", published_at=date(year, 1, 1), sections={}
    )


def test_pick_latest_document_picks_most_recent():
    candidates = [_doc("A-2023", 2023), _doc("A-2025", 2025), _doc("A-2024", 2024)]
    picked = pick_latest_document(candidates)
    assert picked.document_id == "A-2025"


def test_pick_latest_document_with_no_candidates_returns_none():
    assert pick_latest_document([]) is None
