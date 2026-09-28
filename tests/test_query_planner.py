import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from research.query_planner import QueryPlanningError, plan_queries


class QueryPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_deduplication_and_limit(self):
        client = AsyncMock()
        client.chat.return_value = json.dumps({"queries": [
            "  EFFICIENT   MoE inference ", "expert caching", "Expert Caching",
            "expert offloading", "quantization"]})
        plan = await plan_queries("efficient MoE inference", client=client, max_queries=3)
        self.assertEqual(plan.queries, ["efficient MoE inference", "expert caching", "expert offloading"])
        client.chat.assert_awaited_once()
        client.raw.close.assert_not_awaited()

    async def test_original_is_added_when_model_omits_it(self):
        client = AsyncMock()
        client.chat.return_value = '{"queries": ["expert caching"]}'
        plan = await plan_queries("MoE inference", client=client)
        self.assertEqual(plan.queries, ["MoE inference", "expert caching"])

    async def test_invalid_output_is_not_silently_accepted(self):
        for raw in [None, "", "not json", '{"queries": []}',
                    '{"queries": [12]}', '{"queries": [" "]}',
                    '{"queries": "query"}', '{"queries": ["x"], "intent": "y"}']:
            with self.subTest(raw=raw):
                client = AsyncMock()
                client.chat.return_value = raw
                with self.assertRaises(QueryPlanningError):
                    await plan_queries("MoE", client=client)
                client.chat.assert_awaited_once()

    async def test_bad_inputs_do_not_call_model(self):
        client = AsyncMock()
        for query, limit in [(" ", 5), (None, 5), ("MoE", 0), ("MoE", 21), ("MoE", True)]:
            with self.assertRaises(ValueError):
                await plan_queries(query, max_queries=limit, client=client)
        client.chat.assert_not_awaited()

    async def test_owned_client_closed_on_provider_failure(self):
        client = AsyncMock()
        client.chat.side_effect = RuntimeError("provider unavailable")
        with patch("research.query_planner.AsyncLLMClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                await plan_queries("MoE")
        client.raw.close.assert_awaited_once()

    async def test_concurrent_requests_keep_separate_plans(self):
        client = AsyncMock()
        async def respond(prompt, **kwargs):
            await asyncio.sleep(0)
            return json.dumps({"queries": [json.loads(prompt)["query"] + " methods"]})
        client.chat.side_effect = respond
        plans = await asyncio.gather(*(plan_queries(q, client=client) for q in ["MoE", "retrieval"]))
        self.assertEqual([p.queries for p in plans],
                         [["MoE", "MoE methods"], ["retrieval", "retrieval methods"]])


if __name__ == "__main__":
    unittest.main()
