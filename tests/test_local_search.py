from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import bm25s
import faiss
import numpy as np

from indexing.search import LocalSearchIndex, search
from research.retrieval import LocalBackend, RetrievalFilters


class SharedLocalIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        con = sqlite3.connect(self.root / 'metadata.sqlite')
        con.executescript('''
            CREATE TABLE records (row INTEGER, record_id TEXT, paper_id TEXT,
                type TEXT, text TEXT, meta TEXT, source_locations TEXT);
            CREATE TABLE papers (paper_id TEXT, title TEXT, venue TEXT, year INTEGER, folder TEXT);
        ''')
        texts = ['expert caching methods', 'router inference methods', 'expert routing methods']
        for i, text in enumerate(texts):
            con.execute('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (i, f'r{i}', f'p{i}', 'method', text,
                         json.dumps({'year': 2025 + i % 2}), json.dumps([{'page': i+1}])))
            con.execute('INSERT INTO papers VALUES (?, ?, ?, ?, ?)',
                        (f'p{i}', f'Paper {i}', 'ICML', 2025+i % 2, f'folder-{i}'))
        con.commit()
        con.close()
        bm = bm25s.BM25()
        bm.index(bm25s.tokenize(texts, show_progress=False), show_progress=False)
        bm.save(str(self.root / 'bm25'))
        emb_dir = self.root / 'embeddings' / 'fixture'
        emb_dir.mkdir(parents=True)
        (self.root / 'active_embedding').write_text('fixture')
        (emb_dir / 'index_meta.json').write_text(json.dumps({
            'embedding_provider': 'local', 'embedding_model': 'fixture', 'embedding_dim': 2}))
        index = faiss.IndexFlatIP(2)
        index.add(np.array([[1, 0], [0, 1], [.6, .8]], dtype=np.float32))
        faiss.write_index(index, str(emb_dir / 'vectors.faiss'))

    def vector(self, query, *args):
        return np.array([[1, 0] if 'caching' in query else [0, 1]], dtype=np.float32)

    def test_concurrent_cold_load_once_and_rankings_match_sequential(self):
        queries = ['expert caching', 'router inference', 'expert routing', 'caching']
        years = [2025, 2026, None, 2026]
        with patch('indexing.search.embed_query', side_effect=self.vector):
            expected = [search(q, self.root, top_k=3, year_min=y, year_max=y)
                        for q, y in zip(queries, years)]
        barrier = threading.Barrier(4)
        def embed(*args):
            # All four queries must reach embedding concurrently after a single load.
            barrier.wait(timeout=5)
            return self.vector(*args)
        backend = LocalBackend(self.root)
        self.addCleanup(backend.close)
        with patch('indexing.search.faiss.read_index', wraps=faiss.read_index) as load_vector, \
             patch('indexing.search.bm25s.BM25.load', wraps=bm25s.BM25.load) as load_bm25, \
             patch('indexing.search.embed_query', side_effect=embed):
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = [pool.submit(backend.search, q, record_k=3,
                                   filters=RetrievalFilters(year=y))
                        for q, y in zip(queries, years)]
                actual = [job.result(timeout=10) for job in jobs]
            # Reuse across subsequent requests, too.
            with patch('indexing.search.embed_query', side_effect=self.vector):
                again = backend.search(queries[0], record_k=3, filters=RetrievalFilters(year=2025))
            load_vector.assert_called_once()
            load_bm25.assert_called_once()
        for papers, reference in zip(actual, expected):
            self.assertEqual([p.paper_id for p in papers], [p['paper_id'] for p in reference])
            for p, ref in zip(papers, reference):
                self.assertAlmostEqual(p.score, ref['score'])
                self.assertEqual([r.source_locations for r in p.records],
                                 [r['source_locations'] for r in ref['records']])
        self.assertEqual(again, actual[0])

    def test_bm25_only_never_loads_vectors_and_close_releases_indexes(self):
        with patch('indexing.search.faiss.read_index') as load_vector, \
             patch('indexing.search.bm25s.BM25.load', wraps=bm25s.BM25.load) as load_bm25:
            with LocalSearchIndex(self.root, use_vector=False) as index:
                for _ in range(2):
                    self.assertTrue(index.search('expert', top_k=2))
                load_bm25.assert_called_once()
            load_vector.assert_not_called()
        self.assertIsNone(index._lexical)
        self.assertIsNone(index._vector)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            index.search('expert')

    def test_failed_load_can_retry_without_partial_cached_state(self):
        read = faiss.read_index
        attempts = 0
        def fail_once(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError('temporary load failure')
            return read(*args)
        with LocalSearchIndex(self.root) as index, \
             patch('indexing.search.faiss.read_index', side_effect=fail_once), \
             patch('indexing.search.embed_query', side_effect=self.vector):
            with self.assertRaisesRegex(OSError, 'temporary'):
                index.search('expert')
            self.assertIsNone(index._vector)
            self.assertTrue(index.search('expert'))
            self.assertTrue(index.search('expert'))
        self.assertEqual(attempts, 2)

    def test_loaded_embedding_is_pinned_until_new_instance(self):
        with LocalSearchIndex(self.root) as index, \
             patch('indexing.search.embed_query', side_effect=self.vector):
            first = index.search('expert')
            (self.root / 'active_embedding').write_text('missing-variant')
            self.assertEqual(index.search('expert'), first)
            with LocalSearchIndex(self.root) as fresh:
                with self.assertRaises(SystemExit):
                    fresh.search('expert')


if __name__ == '__main__':
    unittest.main()
