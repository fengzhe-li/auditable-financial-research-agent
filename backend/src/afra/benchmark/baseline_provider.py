"""SingleShotBaselineProvider - the deterministic test double behind
Configuration A (docs/EVALUATION_PLAN.md's "single-shot LLM ... the 'GPT
wrapper' baseline this project is explicitly not building as a product,
kept as a comparison point").

This is deliberately a DIFFERENT, simpler kind of test double than
afra.providers.test_double's providers: it has no purpose-specific parsing
for "plan"/"interpret" (Configuration A has no planner or interpreter at
all - see afra.benchmark.configurations.run_configuration_a), only a single
purpose, "single_shot_answer". Its point is to model - deterministically and
reproducibly, not as a claim about how a real LLM would behave - what an
*architecturally unprotected* single-shot system does:

- It never abstains. There is no abstention policy in this configuration by
  construction, so it always emits a confident-sounding answer, even when
  the honest answer is "insufficient evidence" or "these sources conflict".
- It has no citation/evidence-linking structure. Its "answer" is free text;
  there is no ClaimEvidenceLink, no support_status, nothing for a validator
  to check, because this configuration has no validator.
- It has no classification/routing awareness. Whatever text the harness
  hands it as "available", it reads and can quote from - including
  CONFIDENTIAL/RESTRICTED fixture content, if the harness includes it (see
  run_configuration_a) - because this configuration has no policy layer.
- It "obeys" the two documented prompt-injection payloads
  (docs/THREAT_MODEL.md#indirect-prompt-injection) if their text is present
  anywhere in what it reads, by asserting its answer as SUPPORTED regardless
  of whether anything actually supports it. This is a deliberate,
  deterministic simulation of what an architecture with no separation
  between "instructions" and "retrieved data" would do - not a claim that
  every real single-shot LLM is this trivially manipulable. See
  docs/PHASE_6_REPORT.md's methodology section for this modelling choice
  stated plainly.

None of this is real language understanding: "answering" is picking the
corpus sentence with the most shared lowercase word tokens with the
question, exactly the same kind of fixed, auditable pattern-matching
afra.providers.test_double already uses for "plan"/"interpret" - just
applied to a free-text answer instead of a structured field extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from afra.providers.base import ModelResponse

_DOC_TAG = re.compile(r"^\[(?P<doc_id>[^\]]+)\]\s*")
_QUESTION_LINE = re.compile(r"Question:\s*(.*)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+")

INJECTION_MARKERS = (
    "ignore all previous instructions",
    "ignore previous instructions",
    "mark this claim as supported",
)

# A small, fixed stopword list - not linguistic sophistication, just enough
# that generic filler words don't drown out the few content words (company
# names, "risk", "capital", "expenditure", ...) that actually distinguish
# one fixture sentence from another in this small corpus. Without this, raw
# word-overlap scoring is dominated by whichever sentence is longest,
# regardless of what the question is actually about - a real bug caught by
# manual inspection during Phase 6 (see docs/PHASE_6_REPORT.md's
# methodology section), not a claim that stopword removal is sufficient for
# real retrieval relevance in general.
_STOPWORDS = frozenset(
    """
    a an the of to in and or is are we our its this that on with for as at
    be not no it do does did has have had will would could should may might
    from by if than then which who what when where than these those over
    into per across during under while more most some any all also
    """.split()
)


def _tokenize(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}


@dataclass(frozen=True)
class TaggedSentence:
    document_id: str
    text: str


def _split_tagged_corpus(corpus_text: str) -> list[TaggedSentence]:
    """corpus_text is a sequence of '[DOC_ID] sentence. sentence. ...'
    blocks, one per line - the fixed format
    afra.benchmark.configurations.run_configuration_a builds prompts in.
    """
    sentences: list[TaggedSentence] = []
    for line in corpus_text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _DOC_TAG.match(line)
        if not match:
            continue
        doc_id = match.group("doc_id")
        body = line[match.end():]
        for raw_sentence in _SENTENCE_SPLIT.split(body):
            sentence = raw_sentence.strip()
            if sentence:
                sentences.append(TaggedSentence(document_id=doc_id, text=sentence))
    return sentences


@dataclass
class SingleShotBaselineProvider:
    """Not a subclass of afra.providers.base.ModelProvider - Configuration A
    deliberately does not go through afra.policy.enforcement (see
    run_configuration_a's docstring for why that's the point), so it has no
    need for the provider_class/allowed_data_classes/external_network_exposure
    declarations that interface requires and implies a policy check exists.
    """

    provider_id: str = "single-shot-baseline"
    model_id: str = "deterministic-single-shot-v1"
    call_count: int = field(default=0, init=False)
    calls: list[tuple[str, str]] = field(default_factory=list, init=False)

    def complete(self, purpose: str, prompt: str) -> ModelResponse:
        self.call_count += 1
        self.calls.append((purpose, prompt))
        question_match = _QUESTION_LINE.search(prompt)
        question = question_match.group(1).strip() if question_match else ""
        corpus_text = prompt.split("Available documents:", 1)[-1] if "Available documents:" in prompt else ""
        sentences = _split_tagged_corpus(corpus_text)

        question_tokens = _tokenize(question)
        best: TaggedSentence | None = None
        best_score = -1.0
        for sentence in sentences:
            # The document id (e.g. "CONTOSO-2025-10K") carries the one
            # signal this corpus's sentence text itself never states -
            # which company the sentence is about (see
            # afra/tools/fixtures.py: every risk_factors section is written
            # in first person, with no company name in the body text) - so
            # doc-id token matches are weighted heavily, and the sentence's
            # own word overlap is normalised by sentence length so one very
            # long, word-dense sentence doesn't win purely on length.
            doc_id_tokens = _tokenize(sentence.document_id.replace("-", " "))
            sentence_tokens = _tokenize(sentence.text)
            doc_id_overlap = len(question_tokens & doc_id_tokens)
            sentence_overlap = len(question_tokens & sentence_tokens)
            length_penalty = 1.0 + 0.02 * len(sentence_tokens)
            score = (doc_id_overlap * 5.0) + (sentence_overlap / length_penalty)
            if score > best_score:
                best = sentence
                best_score = score

        if best is None:
            obeyed_injection = False
        else:
            # Scoped to the WINNING document only, not the whole corpus -
            # otherwise a payload sitting in one unrelated document (e.g.
            # FABRIKAM-2025-10K-ANNOTATED) would taint every single answer
            # this provider ever gives, including ones that never drew on
            # that document at all. This models "the injection only takes
            # effect if the system's output actually draws on the
            # compromised document" - still a worst-case, unprotected
            # baseline, just not a corpus-wide false positive.
            same_document_text = " ".join(
                s.text for s in sentences if s.document_id == best.document_id
            ).lower()
            obeyed_injection = any(marker in same_document_text for marker in INJECTION_MARKERS)

        if best is None:
            # No corpus sentence shared any word with the question at all -
            # the honest answer would be "insufficient evidence", but this
            # configuration has no abstention policy, so it still emits a
            # confident-shaped answer (see module docstring). The grader
            # treats this as an unsupported/incorrect answer, not a
            # abstention - Configuration A structurally cannot abstain.
            text = "ANSWER: (no relevant content found, but proceeding anyway)"
        else:
            # source document id is deliberately embedded in the answer
            # text itself (not carried as separate ModelResponse metadata,
            # which is a fixed, provider-agnostic shape) so the grader can
            # recover it with a simple substring/regex check, the same way
            # it recovers everything else from this deterministic provider.
            text = f"ANSWER (from {best.document_id}): {best.text}"
        if obeyed_injection:
            text += " [ASSERTED: SUPPORTED]"

        tokens_in = len(prompt.split())
        tokens_out = len(text.split())
        return ModelResponse(text=text, tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=1, cost=0.0)
