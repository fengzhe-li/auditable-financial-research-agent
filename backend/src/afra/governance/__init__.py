"""Policy-as-data / versioned governance specification - see
docs/GOVERNANCE_POLICY.md.

afra.governance.policy defines the GovernancePolicy data model and its
deterministic validator; afra.governance.default_policy defines the one
canonical policy version (v1) that preserves this system's pre-existing,
previously-hardcoded governance behaviour exactly. Nothing in this package
calls a model, interprets free text, or makes a decision by itself - it is
data plus a validator; the existing deterministic enforcement/review/
sufficiency code (afra.policy.enforcement, afra.review.publication_gate,
afra.sufficiency.coverage) reads it, unchanged in its own logic.
"""
