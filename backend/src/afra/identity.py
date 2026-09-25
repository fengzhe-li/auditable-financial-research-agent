"""Local, fixture-based reviewer identities - not an enterprise identity
provider integration. See
docs/CLAIM_EVIDENCE_MODEL.md#identity-scope-portfolio-implementation, whose
original text (Phase 1) explicitly said "who is allowed to act as
governance_admin versus analyst_1 ... is a deployment/configuration concern
this project does not address." Phase 5 narrows that slightly, in one
specific place: the publication gate and submit_review() now check reviewer
identity against this fixed, in-code list, because the Phase 5 spec's
publication gate explicitly requires a "reviewer is authorized" condition
distinct from "reviewer is not the task author" (reviewer_id != created_by
alone was already enforced since Phase 1 and remains a separate check - see
afra.orchestrator.orchestrator.submit_review).

This is still not authentication: nothing here verifies that a caller
claiming to be "reviewer_1" actually is. It is a closed, hardcoded list of
recognised reviewer identities, exactly as simple and local as every other
identity concept in this project - see docs/ROADMAP.md's Phase 5 deviation
entry for why this list exists at all.
"""

from __future__ import annotations

AUTHORIZED_REVIEWER_IDS = frozenset({"reviewer_1", "governance_admin"})

# The subset of AUTHORIZED_REVIEWER_IDS permitted to resolve a
# require_approval SecurityEvent (afra.orchestrator.orchestrator.resolve_security_event).
# Deliberately narrower than AUTHORIZED_REVIEWER_IDS: an ordinary content
# reviewer approving/rejecting research claims is not the same role as a
# governance/compliance sign-off on a flagged security decision.
GOVERNANCE_REVIEWER_IDS = frozenset({"governance_admin"})
