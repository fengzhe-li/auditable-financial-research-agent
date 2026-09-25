"""Direct tests of the frozen benchmark file itself - integrity checks on
backend/benchmark/tasks_v1.json, independent of running anything through
it. See docs/ROADMAP.md's Phase 6 entry: "Add benchmark/evaluation tests
separately from core correctness tests" - this file and its siblings
(test_benchmark_grader.py, test_benchmark_configurations.py) are that
separate suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from afra.benchmark.schema import CATEGORIES, load_benchmark
from afra.domain.enums import Classification
from afra.tools.fixtures import UnknownDocumentError, get_fixture_document

BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "benchmark" / "tasks_v1.json"


@pytest.fixture(scope="module")
def suite():
    return load_benchmark(BENCHMARK_PATH)


def test_benchmark_has_fifty_tasks(suite):
    assert len(suite.tasks) == 50


def test_benchmark_version_is_v1(suite):
    assert suite.version == "v1"


def test_task_ids_are_unique(suite):
    ids = [t.task_id for t in suite.tasks]
    assert len(ids) == len(set(ids))


def test_every_category_is_used_and_valid(suite):
    used = {t.category for t in suite.tasks}
    assert used == CATEGORIES


def test_every_required_document_exists_in_the_fixture_corpus(suite):
    for task in suite.tasks:
        for document_id in task.required_documents:
            get_fixture_document(document_id)  # raises UnknownDocumentError if missing


def test_every_gold_evidence_document_exists_and_quote_span_is_real(suite):
    """The strongest integrity check: every gold_evidence quote_span must be
    an actual substring of the named document's real fixture text - a gold
    label that doesn't even appear in the source document would make the
    whole benchmark meaningless.
    """
    for task in suite.tasks:
        for evidence in task.gold_evidence:
            document = get_fixture_document(evidence.document_id)
            full_text = " ".join(document.sections.values())
            assert evidence.quote_span in full_text, (
                f"{task.task_id}: gold_evidence quote {evidence.quote_span!r} not found in "
                f"{evidence.document_id}"
            )


def test_every_manual_document_override_exists(suite):
    for task in suite.tasks:
        if task.manual_document_override:
            get_fixture_document(task.manual_document_override)


def test_every_manual_conflicting_claim_document_and_quote_exists(suite):
    for task in suite.tasks:
        if task.manual_conflicting_claim:
            for item in task.manual_conflicting_claim["evidence"]:
                document = get_fixture_document(item["document_id"])
                full_text = " ".join(document.sections.values())
                assert item["quote_span"] in full_text


def test_insufficient_and_conflicting_evidence_tasks_are_not_answerable(suite):
    for task in suite.tasks:
        if task.category in ("insufficient_evidence", "conflicting_evidence"):
            assert task.answerable is False, task.task_id


def test_ambiguous_clarification_tasks_require_clarification(suite):
    for task in suite.tasks:
        if task.category == "ambiguous_clarification":
            assert task.requires_clarification is True, task.task_id


def test_non_ambiguous_tasks_do_not_require_clarification(suite):
    for task in suite.tasks:
        if task.category != "ambiguous_clarification":
            assert task.requires_clarification is False, task.task_id


def test_sensitive_data_tasks_have_a_security_classification_and_expected_outcome(suite):
    for task in suite.tasks:
        if task.category == "sensitive_data_routing_policy":
            assert task.security_classification in {c.value for c in Classification}
            assert task.expected_policy_outcome in {"allow", "block", "route_private", "require_approval"}


def test_restricted_or_confidential_security_tasks_with_block_outcome_are_unanswerable(suite):
    for task in suite.tasks:
        if task.category == "sensitive_data_routing_policy" and task.expected_policy_outcome == "block":
            assert task.answerable is False, task.task_id


def test_benchmark_round_trips_through_json(tmp_path, suite):
    from afra.benchmark.schema import save_benchmark

    out = tmp_path / "roundtrip.json"
    save_benchmark(suite, out)
    reloaded = load_benchmark(out)
    assert reloaded.to_dict() == suite.to_dict()
