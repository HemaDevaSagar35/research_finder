"""Deterministic provenance and coverage gates for cross-paper synthesis."""
import json

from novelty.assessment_schemas import TargetCoverage, CoverageRow
from novelty.pipeline import pointer


def pair_claims(pair, relationship):
    return (set(pair.hypothesis_claim_ids) | {x for d in pair.dimensions for x in d.claim_ids}
            | relationship.refs())


def build_packet(inputs, direction_id):
    direction = next(d for d in inputs.generation.directions if d.direction_id == direction_id)
    c = next(c for c in inputs.comparisons.candidates if c.direction_id == direction_id)
    search = next(c for c in inputs.search.candidates if c.direction_id == direction_id)
    invalid = [e for e in inputs.invalidations.entries if e.direction_id == direction_id] if inputs.invalidations else []
    targets = [(direction_id, 'direction')] + [(h.hypothesis_id, 'hypothesis') for h in direction.proposal.hypotheses]
    papers = {p.paper_id: p for p in c.papers}
    coverage, evidence, passages = [], [], {}
    for tid, level in targets:
        rows = []
        for pid in c.shortlist.get(tid, []):
            p = papers[pid]
            pair = next((x for x in p.comparison.pairs if x.target_id == tid), None) if p.comparison else None
            record = p.evidence_reviews[-1].record if p.evidence_reviews else None
            relation = next((r for r in record.relationships if r.target_id == tid), None) if record else None
            refs = pair_claims(pair, relation) if pair and relation else set()
            rejected = [e.reason for e in invalid if e.paper_id == pid and (not e.target_ids or tid in e.target_ids)
                        and (not e.claim_ids or set(e.claim_ids) & refs)]
            status = ('invalidated' if rejected else p.status if p.status in ('skipped', 'failed') else
                      'reviewed' if pair and relation and relation.coverage != 'insufficient_evidence' else 'withheld')
            reasons = rejected or ([p.diagnostic or 'No accepted comparison for this target.'] if status != 'reviewed' else [])
            rows.append(CoverageRow(paper_id=pid, status=status, reasons=reasons, missing_referenced_pages=p.missing_referenced_pages))
            if status == 'reviewed':
                evidence.append(dict(paper_id=pid, target_id=tid, comparison=pair.model_dump(),
                    relationship=relation.model_dump(),
                    claims=[x.model_dump() for x in record.claims if x.claim_id in refs],
                    evidence_scope=p.evidence_scope, missing_referenced_pages=p.missing_referenced_pages))
                # Complete available pages of accepted papers let the independent reviewer
                # flag grounding problems, without accepting new facts at this stage.
                for passage in p.passages:
                    passages[passage.passage_id] = passage.model_dump()
        retrieval = c.retrieval_status.get(tid, 'not_run')
        blockers = []
        if retrieval != 'complete': blockers.append('Retrieval is ' + retrieval + '.')
        if not rows: blockers.append('No shortlisted prior work was compared.')
        if c.diagnostic: blockers.append(c.diagnostic)
        if search.diagnostic: blockers.append(search.diagnostic)
        threshold = inputs.comparison_coverage_threshold or 1.0
        accepted = sum(r.status == 'reviewed' and not r.missing_referenced_pages for r in rows)
        if threshold == 1.0 or not rows or accepted / len(rows) < threshold:
            blockers.extend(f'{r.paper_id}: {r.status}' for r in rows if r.status != 'reviewed')
        if threshold == 1.0 or not rows or accepted / len(rows) < threshold:
            blockers.extend(f'{r.paper_id}: missing referenced pages {r.missing_referenced_pages}' for r in rows if r.missing_referenced_pages)
        coverage.append(TargetCoverage(target_id=tid, level=level, retrieval_status=retrieval,
            papers=rows, blockers=blockers, complete=not blockers).model_dump())
    if inputs.individual_assessments:
        # Preserve full pages containing accepted claims, including surrounding context.
        cited = {pid for e in evidence for claim in e['claims'] for pid in claim['passage_ids']}
        pages = {p.get('page_id') for pid, p in passages.items() if pid in cited}
        passages = {pid:p for pid,p in passages.items() if pid in cited or (p.get('page_id') is not None and p.get('page_id') in pages)}
    packet = dict(direction_id=direction_id, candidate_sha256=c.candidate_sha256,
        candidate=direction.proposal.model_dump(), signature=c.signature.model_dump() if c.signature else None,
        coverage=coverage, evidence=evidence, source_passages=list(passages.values()),
        scope='Saved index retrieval and its complete recorded shortlists; available extracted evidence only. No global novelty proof.',
        retrieval_context=inputs.search.run)
    if inputs.assessment_contract:
        packet["assessment_contract"] = inputs.assessment_contract
        packet["coverage_threshold"] = inputs.comparison_coverage_threshold or 1.0
    return packet



def source_aliases(packet):
    return {p['passage_id']: f'src{i}' for i, p in enumerate(packet['source_passages'])}


def map_source_refs(value, mapping):
    """Translate only typed source-reference fields; scientific prose is untouched."""
    if isinstance(value, list): return [map_source_refs(x, mapping) for x in value]
    if not isinstance(value, dict): return value
    result = {}
    for key, item in value.items():
        if key == 'passage_id': result[key] = mapping.get(item, item)
        elif key == 'passage_ids': result[key] = [mapping.get(x, x) for x in item]
        else: result[key] = map_source_refs(item, mapping)
    return result


def model_packet(packet):
    """Lossless scientific context with short, reversible source labels.

    Full IDs/hashes/offsets remain in immutable inputs. Grouping by original page
    preserves paper identity and page context without repeating long hash IDs for
    every passage. Only source-reference fields use aliases; text is unchanged.
    """
    aliases = source_aliases(packet)
    packed = map_source_refs(packet, aliases)
    pages = {}
    for p in packet['source_passages']:
        # Synthetic controls may supply only passage_id/text.
        page = p.get('page_id', p['passage_id'].split(':')[0])
        pages.setdefault(page, {})[aliases[p['passage_id']]] = p['text']
    packed['source_passages'] = pages
    packed['source_reference_format'] = ('Complete source passages grouped by original page ID; each page maps srcN to its exact passage text. '
        'srcN labels are exact local source identifiers: cite them unchanged in passage_ids. '
        'Code maps them back to immutable original passage IDs; do not reconstruct hashes.')
    return packed


def restore_response_sources(response, packet):
    reverse = {alias: original for original, alias in source_aliases(packet).items()}
    return type(response).model_validate(map_source_refs(response.model_dump(), reverse))


def evidence_map(packet):
    return {(e['paper_id'], e['target_id']): e for e in packet['evidence']}


def validate_draft(draft, packet):
    ledger = {c['target_id']: c for c in packet['coverage']}
    if sorted(t.target_id for t in draft.targets) != sorted(ledger):
        raise ValueError('assessment must cover every original target exactly once')
    available = evidence_map(packet)
    invalid_refs = []
    for ti, target in enumerate(draft.targets):
        for ri, ref in enumerate(target.evidence):
            row = available.get((ref.paper_id, ref.target_id))
            allowed = {c['claim_id'] for c in row['claims']} if row else set()
            missing = set(ref.claim_ids) - allowed
            if row is None or missing:
                invalid_refs.append(dict(field_path=f'/targets/{ti}/evidence/{ri}',
                    paper_id=ref.paper_id, target_id=ref.target_id,
                    error='unavailable paper/target evidence' if row is None else 'claim IDs not accepted for this paper/target',
                    invalid_claim_ids=sorted(missing), allowed_claim_ids=sorted(allowed)))
    if invalid_refs:
        raise ValueError('assessment cites unreviewed, invalidated or unknown evidence: '+json.dumps(invalid_refs))
    for target in draft.targets:
        rows = []
        for ref in target.evidence:
            row = available.get((ref.paper_id, ref.target_id))
            if row is None or not set(ref.claim_ids) <= {c['claim_id'] for c in row['claims']}:
                raise ValueError('assessment cites unreviewed, invalidated or unknown evidence')
            if len(ref.claim_ids) != len(set(ref.claim_ids)):
                raise ValueError('duplicate claim references')
            rows.append(row)
        if target.finding != 'UNRESOLVED' and not any(e['target_id'] == target.target_id for e in rows):
            raise ValueError('substantive finding requires accepted same-target evidence')
        if target.finding == 'LOW_PRIOR_OVERLAP':
            if not ledger[target.target_id]['complete']:
                raise ValueError('low prior overlap requires complete recorded retrieval/comparison coverage')
            if not target.meaningful_difference:
                raise ValueError('low overlap needs a meaningful scientific distinction')
            relevant = [e for e in packet['evidence'] if e['target_id'] == target.target_id]
            if any(e['comparison']['classification'] in ('SAME', 'VERY_CLOSE')
                   or e['relationship']['coverage'] in ('direct_empirical', 'direct_theoretical', 'inferred_implication') for e in relevant):
                raise ValueError('direct/close/inferred prior coverage cannot be promoted to low overlap; resolve upstream or report overlap')
        if target.finding == 'ALREADY_STUDIED':
            matching = [e for e in rows if e['target_id'] == target.target_id]
            if ledger[target.target_id]['level'] == 'hypothesis':
                matching = [e for e in matching if e['relationship']['coverage'] in ('direct_empirical', 'direct_theoretical')]
            else:
                matching = [e for e in matching if e['comparison']['classification'] in ('SAME', 'VERY_CLOSE')]
            if not matching:
                raise ValueError('already studied requires matching direct target evidence, not separate components or discussion')
    r = draft.refinement
    original = {h['hypothesis_id'] for h in packet['candidate']['hypotheses']}
    retained, removed = set(r.retained_hypothesis_ids), set(r.removed_hypothesis_ids)
    if (retained & removed or retained | removed != original or len(retained) != len(r.retained_hypothesis_ids)
            or len(removed) != len(r.removed_hypothesis_ids)):
        raise ValueError('refinement must partition the original hypotheses exactly once')
    if (r.action == 'reject') != (not retained):
        raise ValueError('reject removes all hypotheses; other actions retain at least one')
    if r.action == 'reject' and r.scientific_edits:
        raise ValueError('reject cannot hide a scientific reframe')
    if r.action in ('retain', 'defer') and (removed or r.scientific_edits):
        raise ValueError('retain/defer cannot silently alter the proposal')
    if r.action == 'narrow' and not removed and not r.scientific_edits:
        raise ValueError('narrow needs a removal or an explicit scientific edit')
    if r.action == 'reframe' and not r.scientific_edits:
        raise ValueError('reframe needs explicit scientific edits')
    assessments = {t.target_id: t for t in draft.targets}
    allowed_removals = ('ALREADY_STUDIED', 'UNRESOLVED') if packet.get('assessment_contract') == 'isolated_v1' else ('ALREADY_STUDIED',)
    if any(assessments[h].finding not in allowed_removals and not (packet.get('assessment_contract') == 'isolated_v1' and not ledger[h]['complete']) for h in removed):
        raise ValueError('novelty-based removal requires positive already-studied evidence for each removed hypothesis')
    if r.action == 'retain' and any(not ledger[h]['complete'] or assessments[h].finding == 'UNRESOLVED' for h in retained | ({packet['direction_id']} & set(ledger))):
        raise ValueError('unresolved retained scope must defer, narrow or propose a reframe')
    if packet.get('locked_hypotheses') is not None:
        actual = {t.target_id:t.model_dump() for t in draft.targets if t.target_id != packet['direction_id']}
        expected = {t['target_id']:t for t in packet['locked_hypotheses']}
        if actual != expected:
            raise ValueError('synthesis cannot replace individual hypothesis judgments')
        if r.action in ('retain','narrow') and not r.scientific_edits and any(not ledger[h]['complete'] or assessments[h].finding in ('UNRESOLVED','ALREADY_STUDIED') for h in retained):
            raise ValueError('retained subset needs accepted, evidence-ready hypothesis judgments')
    paths = set()
    scientific = {'research_direction', 'proposed_mechanism', 'scope', 'assumptions', 'what_would_falsify_it'}
    hypothesis_fields = {'condition', 'intervention', 'expected_effect', 'mechanism', 'assumptions', 'falsification_criterion'}
    for edit in r.scientific_edits:
        path = edit.field_path
        parts = path.split('/')[1:]
        valid = len(parts) == 1 and parts[0] in scientific
        if len(parts) == 3 and parts[0] == 'hypotheses' and parts[1].isdigit() and parts[2] in hypothesis_fields:
            index = int(parts[1])
            valid = index < len(packet['candidate']['hypotheses']) and packet['candidate']['hypotheses'][index]['hypothesis_id'] in retained
        if not valid or path in paths:
            raise ValueError('edit needs a unique existing scientific field of a retained target')
        old = pointer(packet['candidate'], path)
        if type(old) != type(edit.proposed_value) or old == edit.proposed_value or edit.proposed_value == []:
            raise ValueError('scientific edit must change a field without changing its value type')
        paths.add(path)


def validate_report(report, packet):
    if sorted(c.target_id for c in report.scope_checks) != sorted(c['target_id'] for c in packet['coverage']):
        raise ValueError('independent scope review must cover every target exactly once')
    available = evidence_map(packet)
    passages = {p['passage_id'] for p in packet['source_passages']}
    for request in report.reassessment_requests:
        rows = [available.get((request.paper_id, tid)) for tid in request.target_ids]
        if any(r is None for r in rows):
            raise ValueError('reassessment must identify accepted source pairs in this packet')
        claims = {c['claim_id'] for r in rows for c in r['claims']}
        if not set(request.claim_ids) <= claims or not set(request.passage_ids) <= passages:
            raise ValueError('unknown reassessment claim or passage')
        if any(not p.startswith(request.paper_id + '#') for p in request.passage_ids):
            raise ValueError('reassessment source belongs to another paper')


def hypothesis_packet(packet, target_id):
    """Keep one original hypothesis and all its accepted comparison evidence."""
    from copy import deepcopy
    scoped = deepcopy(packet)
    scoped['coverage'] = [c for c in packet['coverage'] if c['target_id'] == target_id]
    if len(scoped['coverage']) != 1 or scoped['coverage'][0]['level'] != 'hypothesis':
        raise ValueError('unknown hypothesis target')
    scoped['candidate']['hypotheses'] = [h for h in scoped['candidate']['hypotheses'] if h['hypothesis_id'] == target_id]
    scoped['evidence'] = [e for e in packet['evidence'] if e['target_id'] == target_id]
    cited = {pid for e in scoped['evidence'] for c in e['claims'] for pid in c['passage_ids']}
    pages = {p.get('page_id') for p in packet['source_passages'] if p['passage_id'] in cited}
    scoped['source_passages'] = [p for p in packet['source_passages'] if p['passage_id'] in cited or (p.get('page_id') is not None and p.get('page_id') in pages)]
    scoped['scope'] += ' Assess only the hypothesis listed in coverage; direction context is supplied only for interpretation.'
    return scoped


def hypothesis_draft(target, packet):
    """The model judges novelty; code owns the single-target lifecycle plan."""
    from novelty.assessment_schemas import AssessmentDraft
    complete=packet['coverage'][0]['complete']
    action='reject' if target.finding=='ALREADY_STUDIED' else 'retain' if complete and target.finding!='UNRESOLVED' else 'defer'
    return AssessmentDraft(targets=[target],refinement=dict(action=action,rationale='Single hypothesis judgment; direction composition is reviewed separately.',
        retained_hypothesis_ids=[] if action=='reject' else [target.target_id],removed_hypothesis_ids=[target.target_id] if action=='reject' else [],scientific_edits=[]))


def locked_targets(packet, children):
    from novelty.assessment_schemas import TargetAssessment
    by_id={h.coverage[0].target_id:h for h in children}
    concerns=[q for c in children for r in c.reviews if r.report for q in r.report.reassessment_requests]
    result=[]
    for h in packet['candidate']['hypotheses']:
        tid=h['hypothesis_id']
        child=by_id.get(tid)
        dependent=any(e['target_id']==tid and e['paper_id']==q.paper_id and (not q.claim_ids or set(q.claim_ids)&{c['claim_id'] for c in e['claims']}) for q in concerns for e in packet['evidence'])
        if child and child.assessment and not dependent:
            result.append(child.assessment.targets[0])
        else:
            result.append(TargetAssessment(target_id=tid,finding='UNRESOLVED',reasoning='Individual assessment pending: '+('Shared evidence requires reassessment.' if dependent else child.diagnostic if child and child.diagnostic else 'No accepted judgment.'),evidence=[],meaningful_difference=None))
    return result
