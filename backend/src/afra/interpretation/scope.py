"""ResearchScope - the structured interpretation of a free-text research
question. Not a new database table: persisted as JSON on
ResearchTask.resolved_scope["research_scope"], the same field Phase 2 used
for subquestions - see docs/PHASE_3_REPORT.md for why no new table was
added for this.

Fields:

- companies: entities the question is about
- time_range: which filing period(s) are in scope
- document_types: what kind of document to look in (this fixture corpus
  only has one type - annual reports - so this field is currently always
  defaulted, not really interpreted; see docs/PHASE_3_REPORT.md)
- comparison_axis: what dimension a comparison targets (risk, capex,
  revenue, ...) - the canonical ambiguous field (see
  docs/PROJECT_DEFINITION.md's "Nvidia vs AMD AI exposure" example)
- source_constraints: which sources are allowed (defaulted in Phase 3 -
  Phase 4 is where this becomes a real, enforced policy input)
- source_scope: PUBLIC-only vs. allowed to touch internal sources
  (defaulted in Phase 3, same reason)

Only `companies` and `comparison_axis` are ever classified AMBIGUOUS or
MISSING in Phase 3 - see materially_unresolved_fields(). The other four
fields are always defaulted to RESOLVED with a fixed value: this fixture
corpus doesn't have enough variety (one document type, all-public,
no internal-source concept yet) for interpreting them from free text to
mean anything real yet. Defaulting them honestly, rather than building
interpretation logic with nothing to test it against, is the Phase 3
scope-discipline call - see docs/PHASE_3_REPORT.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from afra.domain.enums import ScopeFieldStatus


@dataclass
class ScopeField:
    status: ScopeFieldStatus
    value: object = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status.value, "value": self.value, "reason": self.reason}

    @staticmethod
    def from_dict(data: dict) -> "ScopeField":
        return ScopeField(
            status=ScopeFieldStatus(data["status"]), value=data.get("value"), reason=data.get("reason")
        )


@dataclass
class ResearchScope:
    companies: ScopeField
    comparison_axis: ScopeField
    time_range: ScopeField = field(
        default_factory=lambda: ScopeField(ScopeFieldStatus.RESOLVED, value="latest available")
    )
    document_types: ScopeField = field(
        default_factory=lambda: ScopeField(ScopeFieldStatus.RESOLVED, value=["annual_report"])
    )
    source_constraints: ScopeField = field(
        default_factory=lambda: ScopeField(ScopeFieldStatus.RESOLVED, value="fixture corpus only")
    )
    source_scope: ScopeField = field(
        default_factory=lambda: ScopeField(ScopeFieldStatus.RESOLVED, value="PUBLIC")
    )

    FIELD_NAMES = (
        "companies",
        "comparison_axis",
        "time_range",
        "document_types",
        "source_constraints",
        "source_scope",
    )

    def to_dict(self) -> dict:
        return {name: getattr(self, name).to_dict() for name in self.FIELD_NAMES}

    @staticmethod
    def from_dict(data: dict) -> "ResearchScope":
        kwargs = {name: ScopeField.from_dict(data[name]) for name in ResearchScope.FIELD_NAMES if name in data}
        return ResearchScope(**kwargs)


def materially_unresolved_fields(scope: ResearchScope) -> list[tuple[str, ScopeField]]:
    """Which fields are unresolved AND actually block progress.

    `companies` is always material - there is no bounded research task
    without knowing which company/companies it's about.

    `comparison_axis` is only material when a comparison was requested (its
    value/reason records that) - a single-company factual question has no
    axis to disambiguate.
    """
    unresolved: list[tuple[str, ScopeField]] = []
    if scope.companies.status != ScopeFieldStatus.RESOLVED:
        unresolved.append(("companies", scope.companies))
    if (
        scope.comparison_axis.status != ScopeFieldStatus.RESOLVED
        and scope.comparison_axis.reason  # only material if the interpreter flagged *why* (comparison requested)
    ):
        unresolved.append(("comparison_axis", scope.comparison_axis))
    return unresolved
