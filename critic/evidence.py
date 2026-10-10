"""Full source packets, deterministic reference validation and publication gates."""
from hashlib import sha256

from critic.schemas import CRITERIA, CritiqueDraft, PortfolioDraft
from novelty.assessment_evidence import build_packet
from novelty.pipeline import pointer
from opportunities.miner import digest
from reasoning.evidence import candidates


def upstream_route(novelty, direction):
    c = next(c for c in novelty.candidates if c.direction_id == direction.direction_id)
    route = c.refinement_handoff(direction)
    if route is None:
        return 'No independently accepted novelty assessment.'
    if route['next_stage'] != 'research_critic' or route['unresolved_target_ids'] or route['requires_novelty_recheck_target_ids']:
        return 'Upstream work remains: ' + str(route)
    return None


def packet_for(novelty, direction, support):
    """Support is loaded and hash-checked through the existing source-store adapter."""
    packet = build_packet(novelty.inputs, direction.direction_id)
    c = next(c for c in novelty.candidates if c.direction_id == direction.direction_id)
    packet['novelty_assessment'] = c.assessment.model_dump()
    packet['novelty_outcomes'] = c.outcomes()
    packet['opportunity'] = direction.opportunity.model_dump()
    packet['support_context'] = support
    validate_support(support, direction)
    for page in support['pages']:
        packet['source_passages'].append(dict(passage_id='support:' + page['page_id'],
            page_id=page['page_id'], paper_id=page['paper_id'], text=page['text']))
    return packet


def validate_support(support, direction):
    if set(support) != {'paper_artifacts', 'pages'}:
        raise ValueError('unexpected support fields')
    artifacts = support['paper_artifacts']
    if {a['paper_id']: a['sha256'] for a in artifacts} != direction.paper_artifact_hashes or len(artifacts) != len(direction.paper_artifact_hashes):
        raise ValueError('support artifact manifest mismatch')
    refs = {f'{p.paper_id}#p{p.page}': p for p in direction.context_pages}
    pages = support['pages']
    if sorted(p['page_id'] for p in pages) != sorted(refs):
        raise ValueError('support page inventory mismatch')
    for p in pages:
        ref = refs[p['page_id']]
        if p['paper_id'] != ref.paper_id or sha256(p['text'].encode()).hexdigest() != ref.sha256:
            raise ValueError('support page hash mismatch')
    rows = {a['paper_id']: {r.value_path: r for r in candidates(a['paper'], a['paper_id'])} for a in artifacts}
    for e in direction.opportunity.evidence:
        row = rows[e.paper_id].get(e.value_path)
        if row is None or not row.source_value.startswith(e.source_value) or row.provenance_path != e.provenance_path:
            raise ValueError('support evidence differs from accepted opportunity')


def references(ids, packet):
    if len(ids) != len(set(ids)) or not set(ids) <= {p['passage_id'] for p in packet['source_passages']}:
        raise ValueError('duplicate or unknown source reference')


def targets(packet):
    all_targets = [packet['direction_id'], *[h['hypothesis_id'] for h in packet['candidate']['hypotheses']]]
    scope = packet.get('review_target_ids')
    if scope is not None:
        if len(scope) != 1 or scope[0] not in all_targets[1:]:
            raise ValueError('independent review must name exactly one original hypothesis')
        return scope
    return all_targets


def valid_pointer(document, path):
    try:
        return pointer(document, path)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError('unknown field path: ' + path) from exc


def validate_request(request, packet):
    if request.target_id not in targets(packet):
        raise ValueError('upstream request names unknown target')
    references(request.passage_ids, packet)
    for path in request.field_paths:
        valid_pointer(packet['candidate'], path)


def validate_draft(draft, packet):
    expected = targets(packet)
    if sorted(t.target_id for t in draft.targets) != sorted(expected):
        raise ValueError('critique must cover direction and every hypothesis exactly once')
    for target in draft.targets:
        if sorted(f.criterion for f in target.findings) != sorted(CRITERIA):
            raise ValueError('each target requires all eight scientific criteria exactly once')
        for finding in target.findings:
            references(finding.passage_ids, packet)
            if finding.basis == 'source_fact' and not finding.passage_ids:
                raise ValueError('source fact requires original evidence')
            if finding.assessment == 'not_applicable' and finding.criterion != 'combination_justification':
                raise ValueError('only component combination can be not applicable')
            for path in finding.proposal_paths:
                valid_pointer(packet['candidate'], path)
        if target.action == 'DISCARD' and not any(f.assessment == 'concern' for f in target.findings):
            raise ValueError('discard requires an identified scientific concern')
    revised = set()
    seen = set()
    for r in draft.revisions:
        if (r.target_id, r.field_path) in seen:
            raise ValueError('duplicate revision')
        seen.add((r.target_id, r.field_path))
        if r.target_id not in expected:
            raise ValueError('revision names unknown target')
        valid_pointer(packet['candidate'], r.field_path)
        parts = r.field_path.split('/')[1:]
        if parts[0] in ('rationale',) or parts[-1] in ('hypothesis_id', 'experiment_id', 'evidence_ids', 'hypothesis_ids'):
            raise ValueError('critic cannot revise immutable IDs, evidence or rationale')
        if not isinstance(valid_pointer(packet['candidate'], r.field_path), (str, list)):
            raise ValueError('revision must address a scientific field, not whole objects')
        if parts[0] == 'hypotheses':
            if len(parts) < 3 or packet['candidate']['hypotheses'][int(parts[1])]['hypothesis_id'] != r.target_id:
                raise ValueError('hypothesis revision must name the matching hypothesis')
        elif parts[0] == 'experiments':
            if len(parts) < 3 or (r.target_id != packet['direction_id'] and
                    r.target_id not in packet['candidate']['experiments'][int(parts[1])]['hypothesis_ids']):
                raise ValueError('test revision must name its direction or a linked hypothesis')
        elif r.target_id != packet['direction_id']:
            raise ValueError('shared scientific field revisions must name the direction')
        revised.add(r.target_id)
    actions = {t.target_id: t.action for t in draft.targets}
    if any(actions[t] != 'REFINE' for t in revised) or any(a == 'REFINE' and t not in revised for t, a in actions.items()):
        raise ValueError('REFINE requires explicit revisions, and revisions require REFINE')
    if 'review_target_ids' not in packet and all(actions[t] == 'DISCARD' for t in expected[1:]) and actions[expected[0]] != 'DISCARD':
        raise ValueError('direction cannot survive with no surviving hypotheses')
    for request in draft.upstream_requests:
        validate_request(request, packet)


def normalize_review_paths(report, draft):
    """Relocate a known flat revision collection, without changing review meaning.

    The draft stores revisions at its root. A model sometimes addresses a target's
    revisions as /targets/N/revisions. Only that exact collection path is eligible,
    and only when that target actually has revision requests. Other paths fail.
    """
    report = report.model_copy(deep=True)
    changes = []
    for issue in report.issues:
        parts = issue.field_path.split('/')
        if (len(parts) == 4 and parts[1] == 'targets' and parts[2].isdigit()
                and parts[3] == 'revisions' and int(parts[2]) < len(draft.targets)):
            target_id = draft.targets[int(parts[2])].target_id
            if any(r.target_id == target_id for r in draft.revisions):
                changes.append(dict(from_path=issue.field_path, to_path='/revisions', target_id=target_id))
                issue.field_path = '/revisions'
    return report, changes


def validate_review(report, draft, packet, portfolio=False, require_test_checks=True):
    expected = packet['eligible_direction_ids'] if portfolio else targets(packet)
    if sorted(c.target_id for c in report.target_checks) != sorted(expected):
        raise ValueError('review must explicitly check every target')
    links = [] if portfolio else [(h, e['experiment_id']) for e in packet['candidate']['experiments'] for h in e['hypothesis_ids'] if 'review_target_ids' not in packet or h in expected]
    actual = [(c.hypothesis_id, c.experiment_id) for c in report.test_link_checks]
    if (require_test_checks or actual) and sorted(actual) != sorted(links):
        raise ValueError('review must check every hypothesis-experiment link exactly once')
    for check in report.test_link_checks:
        if check.decision == 'contradiction' and not any(r.basis == 'proposal_consistency' and r.target_id in (check.hypothesis_id, packet['direction_id']) for r in report.upstream_requests):
            raise ValueError('contradictory test interpretation requires direction-correctness reassessment')
    for issue in report.issues:
        valid_pointer(draft.model_dump(), issue.field_path)
        references(issue.passage_ids, packet)
    for request in report.upstream_requests:
        if portfolio:
            raise ValueError('portfolio source objections must be issues routed to candidate critique')
        validate_request(request, packet)


def portfolio_packet(results):
    eligible = [c for c in results if c.critique is not None and c.handoff()['next_stage'] == 'ranking']
    sources = {}
    dossiers = []
    for c in eligible:
        sources.update({p['passage_id']: p for p in c.packet['source_passages']})
        dossiers.append(dict(direction_id=c.direction_id, candidate=c.packet['candidate'],
            critique=c.critique.model_dump(), novelty_assessment=c.packet['novelty_assessment'],
            novelty_outcomes=c.packet['novelty_outcomes'], evidence=c.packet['evidence'],
            opportunity=c.packet['opportunity'], support_context=c.packet['support_context'],
            passage_ids=[p['passage_id'] for p in c.packet['source_passages']]))
    return dict(eligible_direction_ids=[c.direction_id for c in eligible], candidates=dossiers,
                source_passages=list(sources.values()))


def validate_portfolio(draft, packet):
    if sorted(draft.considered_direction_ids) != sorted(packet['eligible_direction_ids']):
        raise ValueError('portfolio must consider every eligible direction exactly once')
    candidates_by_id = {d['direction_id']: d for d in packet['candidates']}
    used = set()
    for merge in draft.merges:
        references(merge.passage_ids, packet)
        member_sources = set()
        for member in merge.members:
            if member.direction_id not in candidates_by_id or member.direction_id in used:
                raise ValueError('merge has unknown, repeated or overlapping member')
            used.add(member.direction_id)
            d = candidates_by_id[member.direction_id]
            member_sources.update(d['passage_ids'])
            if not set(merge.passage_ids) & set(d['passage_ids']):
                raise ValueError('merge needs source support from every member')
            if sorted(member.hypothesis_ids) != sorted(h['hypothesis_id'] for h in d['candidate']['hypotheses']):
                raise ValueError('merge must preserve all constituent hypotheses by identity')
        if not set(merge.passage_ids) <= member_sources:
            raise ValueError('merge cites evidence outside its constituent directions')


def checked_history(record, packet, portfolio=False, require_test_checks=True):
    validate = validate_portfolio if portfolio else validate_draft
    final = record.assessment if portfolio else record.critique
    if len(record.reviews) > 2 or [r.round for r in record.reviews] != list(range(len(record.reviews))):
        raise ValueError('invalid critic review history')
    for i, r in enumerate(record.reviews):
        if i:
            previous = record.reviews[i - 1]
            if previous.report is None or previous.report.decision != 'revise':
                raise ValueError('correction must follow an actionable revision review')
            if not portfolio:
                from critic.pipeline import preserve_unaffected
                preserve_unaffected(r.draft, previous.draft, previous.report)
        if not isinstance(r.draft, PortfolioDraft if portfolio else CritiqueDraft):
            raise ValueError('wrong review draft type')
        validate(r.draft, packet)
        if r.report:
            validate_review(r.report, r.draft, packet, portfolio, require_test_checks)
    if final is not None:
        validate(final, packet)
        trivial = portfolio and len(packet['eligible_direction_ids']) < 2
        if trivial and (record.reviews or final.merges):
            raise ValueError('trivial portfolio cannot have model reviews or merges')
        if not trivial and (not record.reviews or record.reviews[-1].report is None or
                record.reviews[-1].report.decision != 'pass' or record.reviews[-1].draft != final):
            raise ValueError('published critique must equal latest independently passed draft')
        if any((not portfolio and r.draft.upstream_requests) or (r.report and r.report.upstream_requests) for r in record.reviews):
            raise ValueError('upstream defects must be resolved before publication')
        if record.diagnostic:
            raise ValueError('published critique cannot have unresolved diagnostic')
    elif not record.diagnostic:
        raise ValueError('withheld critique requires diagnostic')


def validate_result(result):
    directions = {d.direction_id: d for d in result.novelty.inputs.generation.directions}
    if sorted(c.direction_id for c in result.candidates) != sorted(directions):
        raise ValueError('critic must preserve every input direction')
    for c in result.candidates:
        d = directions[c.direction_id]
        if c.candidate_sha256 != digest(d.model_dump()):
            raise ValueError('candidate lineage mismatch')
        blocked = upstream_route(result.novelty, d)
        if c.packet is None:
            if c.critique or c.reviews or c.packet_sha256 is not None or not c.diagnostic or c.blocked_at not in ('upstream', 'source_loading'):
                raise ValueError('missing source packet cannot publish critique')
            if (c.blocked_at == 'upstream') != bool(blocked):
                raise ValueError('incorrect upstream routing')
            continue
        if blocked:
            raise ValueError('upstream-ineligible direction cannot receive critique')
        expected = packet_for(result.novelty, d, c.packet['support_context'])
        if c.packet != expected or c.packet_sha256 != digest(expected):
            raise ValueError('critic packet differs from immutable inputs')
        if (c.critique is None) != (c.blocked_at == 'critique'):
            raise ValueError('incorrect critique execution status')
        checked_history(c, c.packet, require_test_checks=result.schema_version != 'research_critic_v1')
    expected = portfolio_packet(result.candidates)
    if result.portfolio.packet != expected or result.portfolio.packet_sha256 != digest(expected):
        raise ValueError('portfolio packet differs from accepted candidate critiques')
    checked_history(result.portfolio, expected, True, result.schema_version != 'research_critic_v1')
