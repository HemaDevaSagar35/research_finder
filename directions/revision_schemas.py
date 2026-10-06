"""Versioned, independently reviewed edits to an existing direction."""
from pydantic import Field, model_validator
from directions.schemas import Strict, Text, Direction, DirectionDraft, CorrectnessIssue
from directions.test_links import LinkedCorrectnessResponse
from opportunities.miner import digest


class Edit(Strict):
    field_path: Text
    previous_value: str | list[str]
    value: str | list[str]
    issue_ids: list[Text] = Field(min_length=1)


class Patch(Strict):
    edits: list[Edit]
    abstention_reason: Text | None


class Resolution(Strict):
    issue_id: Text
    resolved: bool
    reasoning: Text


class RevisionReview(LinkedCorrectnessResponse):
    resolutions: list[Resolution]

    @model_validator(mode='after')
    def resolved_pass(self):
        if self.decision == 'pass' and any(not r.resolved for r in self.resolutions):
            raise ValueError('passing revision review cannot leave requested corrections unresolved')
        return self


class RevisionAttempt(Strict):
    issues: list[CorrectnessIssue]
    patch: Patch
    proposal: DirectionDraft
    review: RevisionReview | None
    error: str | None


class DirectionRevision(Strict):
    original: Direction
    original_sha256: Text
    preflight: LinkedCorrectnessResponse | None
    accepted_refinements: list[dict]
    attempts: list[RevisionAttempt] = Field(max_length=2)
    proposal: DirectionDraft | None
    diagnostic: str | None

    @model_validator(mode='after')
    def exact_reviewed_patch(self):
        from directions.revision import apply_patch, validate_revision_review
        if self.original_sha256 != digest(self.original.model_dump()):
            raise ValueError('original direction hash mismatch')
        current = self.original.proposal
        expected_issues=list(self.preflight.issues) if self.preflight else []
        expected_issues.extend(CorrectnessIssue(category='scope',field_path=r['field_path'],explanation=r['reason'],required_change=r['required_change'],source_spans=[]) for r in self.accepted_refinements)
        if self.attempts and (self.preflight is None or self.preflight.decision == 'abstain'):
            raise ValueError('revision requires a completed non-abstaining preflight')
        if self.preflight:
            from directions.test_links import validate_links
            validate_links(self.preflight.test_link_checks, current)
        for index, attempt in enumerate(self.attempts):
            if index and (self.attempts[index-1].review is None or self.attempts[index-1].review.decision != 'revise'):
                raise ValueError('cannot revise after pass, abstention or failed review')
            if attempt.issues!=expected_issues: raise ValueError('patch issues differ from accepted review/refinements')
            rebuilt = apply_patch(current, attempt.patch, attempt.issues)
            if rebuilt != attempt.proposal:
                raise ValueError('revision differs from recorded field edits')
            if (attempt.review is None) == (attempt.error is None):
                raise ValueError('revision attempt must have review or error')
            if attempt.review:
                validate_revision_review(attempt.review, rebuilt, attempt.issues)
            current = rebuilt
            expected_issues=list(attempt.review.issues) if attempt.review else []
        if self.proposal is not None:
            if not self.attempts or not self.attempts[-1].review or self.attempts[-1].review.decision != 'pass' or current != self.proposal:
                raise ValueError('published revision must equal latest independently passed patch')
            if self.diagnostic:
                raise ValueError('published revision cannot carry unresolved diagnostics')
        elif not self.diagnostic:
            raise ValueError('withheld revision requires diagnostic')
        return self
