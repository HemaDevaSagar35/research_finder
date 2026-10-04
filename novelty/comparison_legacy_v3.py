"""Section-12 v3 contracts: immutable candidate context and source-addressed claims."""
from typing import Literal
import hashlib
from pydantic import Field, model_validator
from directions.schemas import Strict, Text
from reasoning.schemas import ReviewSource
from novelty.schemas import SignatureDraft, TargetSignature
from novelty.comparison_records import PriorClaim, Relationship, EvidenceReview, ReviewFormatRepair

DIMENSIONS = ('problem', 'method', 'mechanism', 'signal', 'regime', 'evaluation', 'scientific_question', 'hypothesis')
Overlap = Literal['SAME', 'VERY_CLOSE', 'PARTIAL_OVERLAP', 'ADJACENT', 'DIFFERENT']


class Passage(Strict):
    passage_id: Text
    page_id: Text
    page_sha256: Text
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: Text

    @model_validator(mode='after')
    def identity(self):
        # Character offsets address decoded page text; hashes bind the source bytes.
        if self.end - self.start != len(self.text):
            raise ValueError('passage offsets must span exact text')
        if self.passage_id != self.make_id(self.page_id, self.page_sha256, self.start, self.end, self.text):
            raise ValueError('passage ID does not match its source/content')
        return self

    @staticmethod
    def make_id(page_id, page_sha256, start, end, text):
        key = f'{page_sha256}:{start}:{end}:{text}'.encode()
        return page_id + ':' + hashlib.sha256(key).hexdigest()[:12]


class DimensionComparison(Strict):
    dimension: Literal['problem', 'method', 'mechanism', 'signal', 'regime', 'evaluation', 'scientific_question', 'hypothesis']
    relation: Literal['SAME', 'CLOSE', 'PARTIAL', 'DIFFERENT', 'UNKNOWN', 'NOT_APPLICABLE']
    claim_ids: list[Text]
    rationale: Text = Field(description='Short interpretation of the referenced claims versus the unchanged candidate; no new prior facts or candidate paraphrase.')

    @model_validator(mode='after')
    def evidence(self):
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError('duplicate claim references')
        if self.relation not in ('UNKNOWN','NOT_APPLICABLE') and not self.claim_ids:
            raise ValueError('determinate dimension comparison requires prior claims')
        return self


class PairComparison(Strict):
    target_id: Text
    dimensions: list[DimensionComparison] = Field(min_length=8, max_length=8)
    classification: Overlap | None
    rationale: Text
    hypothesis_tested: Literal['tested', 'inferred', 'discussed_only', 'not_established', 'not_applicable']
    hypothesis_claim_ids: list[Text]
    additional_uncertainties: list[Text] = Field(description='Only additional comparison limitations. Candidate unknowns are preserved by code; never rewrite or repeat them here.')

    @model_validator(mode='after')
    def dimensions_and_claim(self):
        if sorted(d.dimension for d in self.dimensions) != sorted(DIMENSIONS):
            raise ValueError('all eight dimensions must appear exactly once')
        if self.hypothesis_tested in ('tested','inferred','discussed_only') and not self.hypothesis_claim_ids:
            raise ValueError('tested/discussed hypotheses require prior claims')
        if self.hypothesis_tested in ('not_established','not_applicable') and self.hypothesis_claim_ids:
            raise ValueError('unestablished hypotheses cannot carry claims as proof of testing')
        if self.classification == 'SAME' and any(d.relation not in ('SAME','NOT_APPLICABLE') for d in self.dimensions):
            raise ValueError('SAME requires every applicable dimension to match')
        if self.classification is not None and not any(d.claim_ids for d in self.dimensions):
            raise ValueError('overlap classification requires prior evidence')
        return self


class ComparisonDraft(Strict):
    claims: list[PriorClaim]
    pairs: list[PairComparison] = Field(min_length=1)

    @model_validator(mode='after')
    def references(self):
        claims = {c.claim_id: c for c in self.claims}
        if len(claims) != len(self.claims) or len({p.target_id for p in self.pairs}) != len(self.pairs):
            raise ValueError('duplicate claim or target IDs')
        used = set()
        for pair in self.pairs:
            refs = set(pair.hypothesis_claim_ids) | {c for d in pair.dimensions for c in d.claim_ids}
            if not refs <= claims.keys():
                raise ValueError('unknown prior claim reference')
            used |= refs
            kinds = {claims[c].kind for c in pair.hypothesis_claim_ids}
            if pair.hypothesis_tested == 'tested' and not kinds & {'result','theoretical_result'}:
                raise ValueError('tested hypothesis requires a reported empirical/theoretical result claim')
            if pair.hypothesis_tested == 'discussed_only' and 'discussion' not in kinds:
                raise ValueError('discussed-only hypothesis requires a discussion claim')
        return self


class InterpretationPair(Strict):
    target_id: Text
    dimensions: list[DimensionComparison] = Field(min_length=8,max_length=8)
    classification: Overlap | None
    rationale: Text
    additional_uncertainties: list[Text]


class EvidenceRequest(Strict):
    target_ids: list[Text] = Field(min_length=1)
    reason: Text
    passage_ids: list[Text] = Field(min_length=1)


class ComparisonResponse(Strict):
    pairs: list[InterpretationPair]
    evidence_requests: list[EvidenceRequest]
    abstention_reason: Text | None

    @model_validator(mode='after')
    def exclusive(self):
        if sum(bool(x) for x in (self.pairs,self.evidence_requests,self.abstention_reason))!=1:
            raise ValueError('return pairs, evidence requests, or abstention exclusively')
        return self


class ComparisonIssue(Strict):
    category: Literal['grounding','attribution','consistency','scope','classification']
    field_path: Text
    explanation: Text
    required_change: Text
    passage_ids: list[Text]

    @model_validator(mode='after')
    def support(self):
        if self.category in ('grounding','attribution') and not self.passage_ids:
            raise ValueError('grounding/attribution issues need original prior passages')
        return self


class ComparisonReport(Strict):
    decision: Literal['pass','revise','abstain']
    summary: Text
    issues: list[ComparisonIssue]

    @model_validator(mode='after')
    def consistent(self):
        if self.decision == 'pass' and self.issues:
            raise ValueError('pass cannot contain unresolved issues')
        if self.decision == 'revise' and not self.issues:
            raise ValueError('revise needs actionable issues')
        return self


class ComparisonReview(Strict):
    round: Literal[0,1]
    evidence_version: int = Field(ge=0,le=2)
    draft: ComparisonDraft
    format_repairs: list[ReviewFormatRepair] = Field(default_factory=list,max_length=1)
    model: str | None
    report: ComparisonReport | None
    error: str | None

    @model_validator(mode='after')
    def exclusive(self):
        if (self.report is None) == (self.error is None):
            raise ValueError('review needs report or error exclusively')
        return self


def check_references(draft, targets, passages):
    by_target = {t.target_id:t for t in targets}
    known = {p.passage_id for p in passages}
    if set(by_target) != {p.target_id for p in draft.pairs}:
        raise ValueError('comparison must cover exactly the requested targets')
    for c in draft.claims:
        if not set(c.passage_ids) <= known:
            raise ValueError(f'claim {c.claim_id} references unknown/wrong-paper passage IDs: {sorted(set(c.passage_ids)-known)}')
    for p in draft.pairs:
        if by_target[p.target_id].level == 'hypothesis':
            if p.hypothesis_tested == 'not_applicable':
                raise ValueError('hypothesis target must assess whether the relationship was tested')
            if p.classification == 'SAME' and p.hypothesis_tested not in ('tested','inferred'):
                raise ValueError('SAME hypothesis requires evidence of testing')


class PaperComparison(Strict):
    paper_id: Text
    target_ids: list[Text] = Field(min_length=1)
    targets: list[TargetSignature]
    status: Literal['complete','partial','unresolved','failed','skipped']
    expected_artifact_sha256: Text
    context_pages: list[ReviewSource]
    passages: list[Passage]
    missing_referenced_pages: list[int]
    evidence_scope: Literal['available_extracted_pages'] = 'available_extracted_pages'
    comparison: ComparisonDraft | None
    reviews: list[ComparisonReview]
    evidence_reviews: list[EvidenceReview] = Field(default_factory=list,max_length=3)
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)
    diagnostic: str | None

    @model_validator(mode='after')
    def reviewed(self):
        if len(set(self.target_ids)) != len(self.target_ids) or sorted(self.target_ids) != sorted(t.target_id for t in self.targets):
            raise ValueError('requested targets must equal preserved signature targets')
        keys = {(p.paper_id,p.page):p.sha256 for p in self.context_pages}
        if len(keys)!=len(self.context_pages) or any(p.paper_id != self.paper_id for p in self.context_pages):
            raise ValueError('prior-page manifest must contain unique pages of this paper')
        if set(self.missing_referenced_pages) & {p.page for p in self.context_pages}:
            raise ValueError('page cannot be loaded and missing')
        pages = {f'{p.paper_id}#p{p.page}':p.sha256 for p in self.context_pages}
        if len({p.passage_id for p in self.passages}) != len(self.passages):
            raise ValueError('duplicate passage IDs')
        for p in self.passages:
            if pages.get(p.page_id) != p.page_sha256:
                raise ValueError('passage belongs to a different page or source version')
        from novelty.comparison_evidence import validate_report
        from novelty.comparison_workflow import validate_record, validate_evidence_report, eligible, project, accepted_targets, bind_relationships
        versions={e.version:e for e in self.evidence_reviews}
        if len(versions)!=len(self.evidence_reviews) or list(versions)!=list(range(len(versions))):
            raise ValueError('evidence versions must be consecutive')
        for evidence in self.evidence_reviews:
            validate_record(evidence.record,self.targets,self.passages)
            if evidence.report:validate_evidence_report(evidence.report,evidence.record,self.passages)
        for request in self.evidence_requests:
            if not set(request.target_ids)<=set(self.target_ids) or not set(request.passage_ids)<={p.passage_id for p in self.passages}:
                raise ValueError('invalid evidence reopen request')
        for review in self.reviews:
            ev=versions.get(review.evidence_version)
            if ev is None or ev.report is None:raise ValueError('comparison must bind to an evidence review')
            ids,claims=eligible(ev)
            requested=[t for t in self.targets if t.target_id in ids]
            check_references(review.draft,requested,self.passages)
            if review.draft.claims!=claims:raise ValueError('comparison changed the reviewed evidence claims')
            if bind_relationships(review.draft,ev)!=review.draft:raise ValueError('comparison changed reviewed relationship coverage')
            if review.report:validate_report(review.report,review.draft,self.passages)
        if self.comparison is not None:
            if self.status not in ('complete','partial') or not self.context_pages or not self.passages or not self.reviews:
                raise ValueError('published comparison requires completed evidence-backed review')
            last=self.reviews[-1]
            if last.evidence_version!=len(versions)-1:raise ValueError('comparison uses superseded evidence')
            accepted=accepted_targets(last.report,last.draft,versions[last.evidence_version]) if last.report else set()
            expected=project(last.draft,accepted)
            if expected!=self.comparison:raise ValueError('published pairs must equal independently accepted draft projection')
            complete=len(accepted)==len(self.target_ids)
            if (self.status=='complete')!=complete or (self.diagnostic is None)!=complete:
                raise ValueError('partial coverage must remain explicit')
        elif self.status in ('complete','partial') or not self.diagnostic:
            raise ValueError('withheld comparison needs incomplete status and diagnostic')
        return self

    def outcomes(self):
        from novelty.comparison_workflow import outcomes
        return outcomes(self)

    def evidence_view(self):
        from novelty.comparison_evidence import render_comparison
        return render_comparison(self.comparison,self.targets,self.passages) if self.comparison else None


class CandidateComparison(Strict):
    direction_id: Text
    candidate_sha256: Text
    signature: SignatureDraft | None
    shortlist: dict[str,list[str]]
    retrieval_status: dict[str,Literal['complete','partial','failed']]
    papers: list[PaperComparison]
    diagnostic: str | None

    @model_validator(mode='after')
    def coverage(self):
        if self.signature is None:
            if self.papers or self.shortlist or self.retrieval_status or not self.diagnostic:
                raise ValueError('withheld signature cannot produce comparisons')
            return self
        targets={t.target_id:t for t in self.signature.targets}
        if set(targets)!=set(self.shortlist) or set(targets)!=set(self.retrieval_status):
            raise ValueError('coverage must retain every signature target')
        if any(len(v)!=len(set(v)) for v in self.shortlist.values()):
            raise ValueError('duplicate shortlist paper IDs')
        expected={(tid,pid) for tid,ids in self.shortlist.items() for pid in ids}
        actual=[(tid,p.paper_id) for p in self.papers for tid in p.target_ids]
        if len(actual)!=len(set(actual)) or set(actual)!=expected or len({p.paper_id for p in self.papers})!=len(self.papers):
            raise ValueError('every shortlisted pair needs a result or explicit diagnostic')
        for p in self.papers:
            if any(t!=targets.get(t.target_id) for t in p.targets):
                raise ValueError('preserved candidate differs from reviewed signature')
        return self


class NoveltyComparisonResult(Strict):
    schema_version: Literal['novelty_comparison_v3'] = 'novelty_comparison_v3'
    directions_ref: dict[str,str]
    search_ref: dict[str,str]
    candidates: list[CandidateComparison]
    calls: dict[str,int]
    usage: dict[str,dict[str,int]]
    run: dict
    literature_novelty: Literal['not_assessed'] = 'not_assessed'

    @model_validator(mode='after')
    def unique(self):
        if len({c.direction_id for c in self.candidates}) != len(self.candidates):
            raise ValueError('duplicate comparison candidates')
        return self
