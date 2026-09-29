import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from research.query_planner import QueryPlan
from research.retrieval import (
    LocalBackend, MultiQueryRetriever, OpenSearchBackend, PaperHit, RecordHit,
    RetrievalError, RetrievalFilters, roll_up_records,
)


def paper(pid, score, rid=None, page=1):
    return PaperHit(paper_id=pid, score=score, metadata={"title": pid}, records=[
        RecordHit(record_id=rid or pid + "#method/0", type="method", text="expert caching",
                  score=score, source_locations=[{"page": page}])])


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_rrf_uses_paper_ranks_and_merges_evidence(self):
        backend = Mock()
        backend.search.side_effect = lambda q, **kw: {
            "original": [paper("A", 100), paper("B", 99)],
            "expansion": [paper("C", .9), paper("A", .2, page=2)],
        }[q]
        async with MultiQueryRetriever(backend) as retriever:
            result = await retriever.retrieve(QueryPlan(queries=["original", "expansion"]))
        self.assertEqual([p.paper_id for p in result.papers], ["A", "C", "B"])
        a = result.papers[0]
        self.assertAlmostEqual(a.retrieval_score, 1 / 61 + 1 / 62)
        self.assertEqual([m.rank for m in a.matches], [1, 2])
        self.assertEqual([m.retrieval_score for m in a.matches], [100, .2])
        self.assertEqual(len(a.records), 1)
        self.assertEqual(a.records[0].source_locations, [{"page": 1}, {"page": 2}])
        self.assertEqual([m.query for m in a.records[0].matches], ["original", "expansion"])
        self.assertEqual(json.loads(result.model_dump_json())["queries"], ["original", "expansion"])

    async def test_duplicates_limits_ties_and_filters(self):
        backend = Mock()
        backend.search.return_value = [paper("B", 1), paper("A", 1), paper("A", .5)]
        filters = RetrievalFilters(types=["method"], year=2026)
        async with MultiQueryRetriever(backend) as retriever:
            result = await retriever.retrieve(
                QueryPlan(queries=["MoE inference", " MOE   inference "]),
                paper_k=1, record_k=30, rrf_k=10, filters=filters)
        backend.search.assert_called_once_with("MoE inference", record_k=30, filters=filters)
        self.assertEqual(result.papers[0].paper_id, "A")
        self.assertAlmostEqual(result.papers[0].retrieval_score, 1 / 11)
        self.assertEqual(len(result.papers[0].matches), 1)

    async def test_empty_results_and_invalid_inputs(self):
        backend = Mock()
        backend.search.return_value = []
        async with MultiQueryRetriever(backend) as retriever:
            self.assertEqual((await retriever.retrieve(QueryPlan(queries=["x"]))).papers, [])
            for kwargs in ({"paper_k": 0}, {"record_k": True}, {"rrf_k": -1},
                           {"timeout": 0}, {"timeout": float("nan")}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await retriever.retrieve(QueryPlan(queries=["x"]), **kwargs)
            with self.assertRaises(ValueError):
                await retriever.retrieve(QueryPlan(queries=[" "]))
        with self.assertRaises(RuntimeError):
            await retriever.retrieve(QueryPlan(queries=["x"]))
        self.assertEqual(backend.search.call_count, 1)

    async def test_failure_is_explicit_and_client_is_caller_owned(self):
        backend = Mock()
        backend.search.side_effect = RuntimeError("backend unavailable")
        async with MultiQueryRetriever(backend) as retriever:
            with self.assertRaisesRegex(RetrievalError, "'x'") as caught:
                await retriever.retrieve(QueryPlan(queries=["x"]))
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        backend.close.assert_not_called()

    async def test_concurrent_requests_share_worker_limit_and_keep_state_separate(self):
        lock = threading.Lock()
        release = threading.Event()
        started = threading.Event()
        active = peak = 0

        def search(query, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
            release.wait(5)
            with lock:
                active -= 1
            return [paper(query, 1)]

        backend = Mock(search=search)
        async with MultiQueryRetriever(backend, concurrency=2) as retriever:
            requests = [asyncio.create_task(retriever.retrieve(QueryPlan(queries=qs)))
                        for qs in (["a", "b"], ["c", "d"])]
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
            finally:
                release.set()
            results = await asyncio.gather(*requests)
        self.assertEqual(peak, 2)
        self.assertEqual([{p.paper_id for p in r.papers} for r in results],
                         [{"a", "b"}, {"c", "d"}])

    async def test_deadline_keeps_worker_bounded_and_shutdown_drains(self):
        release = threading.Event()
        started = threading.Event()
        def search(*args, **kwargs):
            started.set()
            release.wait(5)
            return []
        backend = Mock(search=search)
        retriever = MultiQueryRetriever(backend, concurrency=1)
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await retriever.retrieve(QueryPlan(queries=["x"]), timeout=.05)
            self.assertTrue(started.is_set())
        finally:
            release.set()
            await retriever.aclose()


class AdapterTests(unittest.TestCase):
    def test_rollup_deduplicates_records_with_decay(self):
        def record(rid, score):
            return dict(paper_id="A", record_id=rid, type="method", text="x", score=score)
        result = roll_up_records([record("r1", 1), record("r1", 1), record("r2", .5)])
        self.assertEqual(result[0].score, 1.25)
        self.assertEqual(len(result[0].records), 2)

    def test_opensearch_adapter_rollup_and_filters(self):
        client = Mock()
        hits = [dict(paper_id="A", record_id="r1", type="method", text="x",
                     rrf_score=.1, title="Paper A", source_locations=[{"page": 2}]),
                dict(paper_id="A", record_id="r2", type="method", text="y", rrf_score=.08)]
        with patch("indexing.opensearch_index.search", return_value=hits) as search:
            result = OpenSearchBackend(client).search(
                "query", record_k=50, filters=RetrievalFilters(types=["method"], year=2026))
        search.assert_called_once_with(client, "query", k=50, types=["method"], year=2026)
        self.assertAlmostEqual(result[0].score, .14)
        self.assertEqual(result[0].records[0].source_locations, [{"page": 2}])
        client.close.assert_not_called()

    def test_local_search_real_sqlite_preserves_evidence_and_filters(self):
        # Real metadata lookup and paper rollup; stub only the lexical index I/O.
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            con = sqlite3.connect(Path(folder) / "metadata.sqlite")
            con.executescript('''
                CREATE TABLE records (row INTEGER, record_id TEXT, paper_id TEXT,
                    type TEXT, text TEXT, meta TEXT, source_locations TEXT);
                CREATE TABLE papers (paper_id TEXT, title TEXT, venue TEXT, year INTEGER, folder TEXT);
            ''')
            con.execute("INSERT INTO records VALUES (0, 'r1', 'A', 'method', 'expert caching', ?, ?)",
                        (json.dumps({"year": 2026}), json.dumps([{"page": 3}])))
            con.execute("INSERT INTO papers VALUES ('A', 'Paper A', 'ICML', 2026, 'paper-a')")
            con.commit()
            con.close()
            lexical = Mock()
            lexical.retrieve.return_value = (np.array([[0]]), np.array([[1.0]]))
            with patch("indexing.search.bm25s.BM25.load", return_value=lexical):
                backend = LocalBackend(Path(folder), use_vector=False)
                result = backend.search("expert caching", record_k=10,
                                        filters=RetrievalFilters(year=2026))
                empty = backend.search("expert caching", record_k=10,
                                       filters=RetrievalFilters(year=2025))
            self.assertEqual(result[0].records[0].source_locations, [{"page": 3}])
            self.assertEqual(result[0].metadata["folder"], "paper-a")
            self.assertEqual(empty, [])


if __name__ == "__main__":
    unittest.main()
