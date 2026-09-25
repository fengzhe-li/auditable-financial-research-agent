"""Pure routing-decision functions: given a subquestion and the resolved
scope, decide which companies it targets and which document section to
retrieve. No I/O here - the orchestrator executes these decisions by
calling search_documents()/retrieve_section() and persists the decision
(with rationale) as a ToolCall - see
Orchestrator.route_and_gather_evidence in orchestrator.py.

This is deliberately the entire "planner decision layer" requested for
Phase 3 - not a generic planning/agent framework. It answers exactly two
questions per subquestion: which companies, and which document section.
It does not replan, does not backtrack, does not consider more than the
latest filing per company. See docs/PHASE_3_REPORT.md's overengineering
section for why nothing more elaborate was built.
"""

from __future__ import annotations

from afra.tools.fixtures import FixtureDocument

AXIS_TO_SECTION = {
    "risk": "risk_factors",
    "capital expenditure": "risk_factors",
    "revenue": "risk_factors",
}
DEFAULT_SECTION = "risk_factors"


def decide_target_companies(subquestion: str, known_companies: list[str]) -> tuple[list[str], str]:
    """Which known companies does this subquestion concern, and why.

    If the subquestion names one or more known companies explicitly, route
    to exactly those. Otherwise (a subquestion that doesn't name a specific
    company - e.g. a pure "how do these compare" framing that already had
    its comparison_axis resolved elsewhere) it's treated as spanning every
    company in scope, since there's nothing more specific to narrow it to.
    """
    matched = [c for c in known_companies if c.lower() in subquestion.lower()]
    if matched:
        rationale = f"subquestion explicitly names: {', '.join(matched)}"
        return matched, rationale
    rationale = (
        "subquestion does not name a specific company; treated as spanning "
        f"all {len(known_companies)} companies in scope"
    )
    return list(known_companies), rationale


def section_for_axis(axis: str | None) -> str:
    if axis and axis in AXIS_TO_SECTION:
        return AXIS_TO_SECTION[axis]
    return DEFAULT_SECTION


def pick_latest_document(candidates: list[FixtureDocument]) -> FixtureDocument | None:
    """Phase 3's document-selection rule: the most recent filing per
    company. This is the concrete meaning of the (currently always-default)
    ResearchScope.time_range="latest available" - see
    afra.interpretation.scope module docstring for why time_range isn't
    interpreted from free text yet.
    """
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.published_at)
