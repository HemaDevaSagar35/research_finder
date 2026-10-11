"""Query-to-portfolio orchestration with validated, resumable stage checkpoints."""
import asyncio
from llm_client.progress import stage_progress
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from directions.schemas import Strict, GenerationResult
from landscape.schemas import Landscape
from landscape.paper_context import PaperContext, _project
from landscape.builder import build_landscape
from reasoning.schemas import CrossPaperReasoning
from reasoning.cross_paper import CrossPaperReasoner, Budgets
from reasoning.evidence import PaperStore
from opportunities.miner import OpportunityMiner, Settings as MiningSettings, digest
from opportunities.schemas import MiningResult
from directions.generator import DirectionGenerator, Settings as DirectionSettings
from novelty.pipeline import NoveltySearcher, Settings as SearchSettings
from novelty.schemas import NoveltySearchResult
from novelty.comparison import NoveltyComparator, Settings as ReviewSettings
from novelty.comparison_schemas import NoveltyComparisonResult
from novelty.assessment import NoveltyAssessor, AssessmentSettings
from novelty.assessment_schemas import NoveltyAssessmentResult
from critic.pipeline import ResearchCritic
from critic.schemas import CriticResult
from critic.refinement_loop import RefinementLoop, RefinementResult
from portfolio import build_portfolio, FinalPortfolio, SelectionSettings
from portfolio.render import markdown
from research.query_planner import plan_queries, QueryPlan
from research.retrieval import (LocalBackend, OpenSearchBackend, MultiQueryRetriever,
                                RetrievalResult, RetrievalFilters)


class PipelineConfig(Strict):
    query: str = Field(min_length=1)
    root: Path = Path('markdown')
    backend: Literal['local', 'opensearch'] = 'local'
    index_dir: Path = Path('index')
    embedding: str | None = None
    metadata_paths: list[Path] = Field(default_factory=list)
    allow_s3: bool = True
    provider: str | None = None
    model: str | None = None
    review_model: str | None = None
    concurrency: int = Field(default=100, ge=1, le=100)
    max_queries: int = Field(default=5, ge=1, le=20)
    initial_papers: int = Field(default=15, ge=1)
    record_k: int = Field(default=50, ge=1)
    retrieval_timeout: float = Field(default=120, gt=0, allow_inf_nan=False)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    reasoning: Budgets = Field(default_factory=Budgets)
    opportunities: MiningSettings = Field(default_factory=lambda: MiningSettings(max_candidates_per_batch=8))
    directions: DirectionSettings = Field(default_factory=DirectionSettings)
    novelty_search: SearchSettings = Field(default_factory=SearchSettings)
    comparison: ReviewSettings = Field(default_factory=ReviewSettings)
    assessment: AssessmentSettings = Field(default_factory=AssessmentSettings)
    critic: ReviewSettings = Field(default_factory=ReviewSettings)
    refinement: ReviewSettings = Field(default_factory=ReviewSettings)
    max_refinement_cycles: int = Field(default=2, ge=0, le=2)
    selection: SelectionSettings = Field(default_factory=SelectionSettings)


class Contexts(Strict):
    papers: dict[str, PaperContext]
    artifact_sha256: dict[str, str]


STAGES = {
    'plan': QueryPlan, 'retrieval': RetrievalResult, 'contexts': Contexts,
    'landscape': Landscape, 'reasoning': CrossPaperReasoning,
    'opportunities': MiningResult, 'directions': GenerationResult,
    'novelty_search': NoveltySearchResult, 'comparison': NoveltyComparisonResult,
    'assessment': NoveltyAssessmentResult, 'critic': CriticResult,
    'refinement': RefinementResult, 'portfolio': FinalPortfolio,
}


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def code_identity():
    root = Path(__file__).resolve().parent.parent
    files = [p for name in ('research', 'landscape', 'reasoning', 'opportunities',
             'directions', 'novelty', 'critic', 'portfolio', 'llm_client', 'indexing', 'extraction', 'ingestion')
             for p in (root/name).glob('*.py')]
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)})


def environment_identity():
    # Record a fingerprint, never credentials or endpoint strings. Stage-specific
    # model/provider settings still apply when an explicit override is absent.
    names = {k for k in os.environ if k.endswith(('_MODEL', '_PROVIDER'))}
    names.update(('PROVIDER', 'S3_ARTIFACTS_URL', 'OPENSEARCH_HOST', 'OPENSEARCH_INDEX'))
    names.update(k for k in os.environ if k.endswith('_CONTEXT_TOKEN_LIMIT'))
    return digest({k: os.environ.get(k) for k in sorted(names)})


class StageRunner:
    """Owns one store and lazily opens one retriever for the entire run."""
    def __init__(self, config):
        self.config = config
        factory = None
        if config.allow_s3 and os.getenv('S3_ARTIFACTS_URL'):
            from ingestion.s3store import ArtifactStore
            factory = ArtifactStore
        self.store = PaperStore(config.root, store_factory=factory)
        self.backend = self.retriever = self.search_client = None

    def get_retriever(self):
        if self.retriever is None:
            if self.config.backend == 'local':
                self.backend = LocalBackend(self.config.index_dir, embedding=self.config.embedding)
            else:
                from indexing.opensearch_index import get_client
                self.search_client = get_client()
                self.backend = OpenSearchBackend(self.search_client)
            self.retriever = MultiQueryRetriever(self.backend, concurrency=self.config.concurrency)
        return self.retriever

    async def close(self):
        try:
            if self.retriever:
                await self.retriever.aclose()
        finally:
            if isinstance(self.backend, LocalBackend):
                self.backend.close()
            if self.search_client:
                self.search_client.close()

    async def execute(self, name, state):
        c = self.config
        common = dict(provider=c.provider, model=c.model, review_model=c.review_model)
        if name == 'plan':
            return await plan_queries(c.query, max_queries=c.max_queries, provider=c.provider, model=c.model)
        if name == 'retrieval':
            return await self.get_retriever().retrieve(state['plan'], paper_k=c.initial_papers,
                record_k=c.record_k, filters=c.filters, timeout=c.retrieval_timeout)
        if name == 'contexts':
            papers, hashes = {}, {}
            for paper in state['retrieval'].papers:
                loaded = await asyncio.to_thread(self.store.get, paper.paper_id)
                if loaded.status != 'loaded':
                    raise ValueError(f'Cannot load retrieved paper {paper.paper_id}: {loaded.error}')
                card, missing = _project(paper.paper_id, loaded.paper)
                papers[paper.paper_id] = PaperContext(paper_id=paper.paper_id, card=card,
                    missing_fields=missing, matched_records=[r.model_dump() for r in paper.records])
                hashes[paper.paper_id] = loaded.sha256
            return Contexts(papers=papers, artifact_sha256=hashes)
        if name == 'landscape':
            return await build_landscape(c.query, state['contexts'].papers,
                extraction_provider=c.provider, normalize_provider=c.provider, aggregation_provider=c.provider, model=c.model, concurrency=c.concurrency)
        if name == 'reasoning':
            from llm_client import AsyncLLMClient
            client = AsyncLLMClient(c.provider or os.getenv('REASON_PROVIDER'), concurrency=c.concurrency, max_retries=0)
            try:
                return await CrossPaperReasoner(self.store, client.chat_result, budgets=c.reasoning,
                    provider=client.provider, draft_model=c.model, review_model=c.review_model,
                    concurrency=c.concurrency).run(state['landscape'])
            finally:
                await client.raw.close()
        if name == 'opportunities':
            return await OpportunityMiner(self.store, settings=c.opportunities, **common).run(state['landscape'], state['reasoning'])
        if name == 'directions':
            return await DirectionGenerator(self.store, settings=c.directions, **common).run(
                state['landscape'], state['reasoning'], state['opportunities'])
        if name == 'novelty_search':
            return await NoveltySearcher(self.store, self.get_retriever(), settings=c.novelty_search,
                corpus_id=str(c.index_dir) if c.backend == 'local' else 'configured-opensearch',
                retrieval_mode=c.backend, **common).run(state['directions'])
        if name == 'comparison':
            return await NoveltyComparator(self.store, settings=c.comparison, **common).run(state['directions'], state['novelty_search'])
        if name == 'assessment':
            return await NoveltyAssessor(settings=c.assessment, **common).run(
                state['directions'], state['novelty_search'], state['comparison'])
        if name == 'critic':
            result = await ResearchCritic(self.store, settings=c.critic, **common).run(state['assessment'],**({'recovery':state['critic']} if state.get('critic') is not None else {}))
            from critic.hypotheses import review_hypotheses
            self.hypothesis_reviews = await review_hypotheses(state['assessment'], self.store,
                existing=result, recovery=getattr(self,'hypothesis_reviews',None), critic_factory=ResearchCritic, settings=c.critic, **common)
            return result
        if name == 'refinement':
            refined = await RefinementLoop(self.store, settings=c.refinement, retriever=self.get_retriever(),
                max_cycles=c.max_refinement_cycles, search_settings=c.novelty_search, **common).run(state['critic'])
            if refined.repair_rounds or refined.cycles:
                from portfolio.pipeline import current_critic
                from critic.hypotheses import review_hypotheses
                current=current_critic(refined)
                self.hypothesis_reviews=await review_hypotheses(current.novelty,self.store,existing=current,recovery=getattr(self,'hypothesis_reviews',None),settings=c.critic,**common)
            return refined
        if name == 'portfolio':
            paths = list(c.metadata_paths)
            if c.backend == 'local':
                for path in (c.index_dir/'papers.jsonl', c.index_dir.parent/'papers'/'metadata.json'):
                    if path.exists() and path not in paths:
                        paths.append(path)
            return await asyncio.to_thread(build_portfolio, state.get('refinement', state['critic']),
                                           c.selection, metadata_paths=paths, store=self.store)
        raise ValueError(f'Unknown stage: {name}')


@contextmanager
def run_lock(directory):
    with (directory/'.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another process is using this run directory') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def run_pipeline(config, out, *, resume=False, runner_factory=StageRunner, progress=None):
    """Run once, or reuse an unchanged prefix of validated saved checkpoints.

    No model output is promoted by the runner. A completed blocked stage is a
    checkpoint too; resume does not rerun it or grant more refinement cycles.
    """
    config = PipelineConfig.model_validate(config.model_dump())
    config.root = config.root.resolve()
    config.index_dir = config.index_dir.resolve()
    config.metadata_paths = [p.resolve() for p in config.metadata_paths]
    out = Path(out).resolve()
    if resume:
        if not out.is_dir():
            raise ValueError('Resume requires an existing run directory')
    else:
        out.mkdir(parents=True, exist_ok=False)
    identity = dict(schema_version='query_pipeline_v1', config=config.model_dump(mode='json'),
                    code_sha256=code_identity(), environment_sha256=environment_identity())
    with run_lock(out):
        manifest = out/'manifest.json'
        if resume:
            if json.loads(manifest.read_text()) != identity:
                raise ValueError('Resume configuration, environment or code differs from the original run')
        else:
            atomic_json(manifest, identity)
        stages = [s for s in STAGES if s != 'refinement' or config.max_refinement_cycles]
        state, parent = {}, digest(identity)
        runner = runner_factory(config)
        stage = None
        try:
            # Check all saved artifacts before starting any new provider work.
            missing = False
            for name in stages:
                path = out/f'{name}.json'
                if not path.exists():
                    missing = True
                    continue
                if missing:
                    raise ValueError('Checkpoint gap: later stage exists without its parent')
                payload = json.loads(path.read_text())
                if payload['stage'] != name or payload['parent_sha256'] != parent or payload['result_sha256'] != digest(payload['result']):
                    raise ValueError(f'Checkpoint lineage mismatch: {name}')
                state[name] = STAGES[name].model_validate(payload['result'])
                parent = digest(payload)
            for stage in stages:
                reused = stage in state
                if progress:
                    progress(stage, 'reused' if reused else 'running')
                if not reused:
                    try:
                        async with stage_progress(stage, progress):
                            result = await runner.execute(stage, state)
                        result = STAGES[stage].model_validate(result.model_dump())
                        data = result.model_dump(mode='json')
                        payload = dict(stage=stage, parent_sha256=parent, result_sha256=digest(data), result=data)
                        atomic_json(out/f'{stage}.json', payload)
                        parent = digest(payload)
                        state[stage] = result
                        if stage in ('critic','refinement') and hasattr(runner, 'hypothesis_reviews'):
                            atomic_json(out/'hypothesis_reviews.json', runner.hypothesis_reviews)
                    except BaseException:
                        if progress:
                            progress(stage, 'failed')
                        raise
                    if progress:
                        progress(stage, 'completed')
                if stage == 'retrieval' and not state[stage].papers:
                    summary = dict(status='empty', stopped_at=stage, reason='No matching papers retrieved.', portfolio=None)
                    break
                if stage == 'directions' and not state[stage].directions:
                    summary = dict(status='blocked' if state[stage].diagnostics else 'empty', stopped_at=stage,
                        reason='No reviewed directions available.', portfolio=None,
                        diagnostics=[d.model_dump() for d in state[stage].diagnostics])
                    break
            else:
                result = state['portfolio']
                atomic_json(out/'final_portfolio.json', result.model_dump(mode='json'))
                temp = out/'final_portfolio.md.tmp'
                report = markdown(result)
                if (out/'hypothesis_reviews.json').exists():
                    from critic.hypotheses import markdown as hypothesis_markdown
                    report += '\n' + hypothesis_markdown(json.loads((out/'hypothesis_reviews.json').read_text()))
                temp.write_text(report)
                temp.replace(out/'final_portfolio.md')
                summary = dict(status=result.status, stopped_at='portfolio', counts=result.counts,
                    selection_shortfall=result.selection_shortfall, portfolio='final_portfolio.json',
                    diagnostics=result.diagnostics)
            summary['completed_stages'] = list(state)
            atomic_json(out/'summary.json', summary)
            return summary
        except BaseException as exc:
            atomic_json(out/'summary.json', dict(status='failed', stopped_at=stage,
                completed_stages=list(state), error=f'{type(exc).__name__}: {exc}'))
            raise
        finally:
            await runner.close()
