import json
import unittest

import httpx

from niya.llm.openrouter import LLMError, OpenRouterClient, apply_cache_control, list_models


def chat_body(content="嗨", tool_calls=None, usage=None):
    return {
        "model": "test/model",
        "choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 12, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 8}, "cost": 0.0001},
    }


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def make(self, handler, **kwargs):
        self.records = []
        self.sleeps = []

        async def sleep(seconds):
            self.sleeps.append(seconds)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return OpenRouterClient("test-key", http=http, recorder=self.records.append, sleep=sleep, **kwargs)

    async def test_chat_request_and_tool_call_parsing(self):
        def handle(request):
            self.assertEqual(request.url.path, "/api/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "test/model")
            self.assertTrue(body["parallel_tool_calls"])
            self.assertEqual(body["usage"], {"include": True})
            self.assertEqual(body["tool_choice"], "auto")
            calls = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": {"command": "ls"}}}]
            return httpx.Response(200, json=chat_body(content=None, tool_calls=calls))

        client = self.make(handle)
        tools = [{"type": "function", "function": {"name": "terminal", "description": "", "parameters": {}}}]
        result = await client.chat(model="test/model", messages=[{"role": "user", "content": "hi"}], tools=tools, tool_choice="auto")
        self.assertIsNone(result.content)
        self.assertEqual(result.tool_calls[0].name, "terminal")
        self.assertEqual(json.loads(result.tool_calls[0].arguments), {"command": "ls"})
        self.assertEqual((result.usage.prompt_tokens, result.usage.cached_tokens, result.usage.cost_usd), (12, 8, 0.0001))
        self.assertTrue(self.records[0].ok)

    async def test_retries_on_429_with_retry_after(self):
        responses = [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(503), httpx.Response(200, json=chat_body())]

        client = self.make(lambda request: responses.pop(0))
        result = await client.chat(model="test/model", messages=[])
        self.assertEqual(result.content, "嗨")
        self.assertEqual(self.sleeps, [3.0, 2.0])

    async def test_error_inside_200_body(self):
        client = self.make(lambda request: httpx.Response(200, json={"error": {"code": 400, "message": "bad model"}}))
        with self.assertRaisesRegex(LLMError, "bad model"):
            await client.chat(model="test/model", messages=[])
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.records[-1].ok)

    async def test_gives_up_after_max_retries(self):
        client = self.make(lambda request: httpx.Response(502), max_retries=2)
        with self.assertRaises(LLMError):
            await client.chat(model="test/model", messages=[])
        self.assertEqual(len(self.sleeps), 2)

    async def test_transport_errors_are_retried(self):
        attempts = []

        def handle(request):
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json=chat_body())

        result = await self.make(handle).chat(model="test/model", messages=[])
        self.assertEqual(result.content, "嗨")
        self.assertEqual(len(attempts), 2)

    async def test_embeddings_are_returned_in_input_order(self):
        def handle(request):
            self.assertEqual(json.loads(request.content)["input"], ["a", "b"])
            return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})

        vectors = await self.make(handle).embed(model="emb/model", texts=["a", "b"])
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])

    async def test_context_length_lookup_is_cached(self):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"data": [{"id": "test/model", "context_length": 200000}]})

        client = self.make(handle)
        self.assertEqual(await client.context_length("test/model"), 200000)
        self.assertIsNone(await client.context_length("other/model"))
        self.assertEqual(await client.context_length("test/model"), 200000)
        self.assertEqual(calls, ["/api/v1/models", "/api/v1/models"])


    async def test_reasoning_effort_is_sent_only_when_set(self):
        bodies = []

        def handle(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=chat_body())

        client = self.make(handle)
        await client.chat(model="m", messages=[{"role": "user", "content": "hi"}], reasoning={"effort": "high"})
        await client.chat(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(bodies[0]["reasoning"], {"effort": "high"})
        self.assertNotIn("reasoning", bodies[1])

    async def test_deepseek_turns_thinking_on_with_its_own_effort_field(self):
        from niya.sleep.llm import DeepSeekCompleter

        bodies = []

        def handle(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=chat_body(content='{"ok": true}'))

        client = self.make(handle, base_url="https://api.deepseek.com/v1", name="DeepSeek")
        self.assertEqual(await DeepSeekCompleter(client, "deepseek-flash", "xhigh").complete(system="s", prompt="p", task="nrem"), '{"ok": true}')
        await DeepSeekCompleter(client, "deepseek-flash").complete(system="s", prompt="p", task="nrem")
        self.assertEqual((bodies[0]["thinking"], bodies[0]["reasoning_effort"]), ({"type": "enabled"}, "max"))
        self.assertNotIn("reasoning", bodies[0])
        self.assertNotIn("reasoning_effort", bodies[1])

    async def test_errors_name_the_service(self):
        client = self.make(lambda request: httpx.Response(401, text="bad key"), name="DeepSeek")
        with self.assertRaisesRegex(LLMError, "DeepSeek 回應 HTTP 401"):
            await client.chat(model="m", messages=[{"role": "user", "content": "hi"}])

    async def test_public_model_catalog(self):
        def handle(request):
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, json={"data": [
                {"id": "a/tools", "name": "A", "context_length": 1000000, "pricing": {"prompt": "0.000002", "completion": "0.00001"}, "supported_parameters": ["tools", "reasoning"]},
                {"id": "b/plain", "name": "B", "context_length": 8192, "pricing": {}, "supported_parameters": ["temperature"]},
            ]})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(http.aclose)
        models = await list_models(http=http)
        self.assertEqual([(m.id, m.tools) for m in models], [("a/tools", True), ("b/plain", False)])
        self.assertAlmostEqual(models[0].prompt_price, 2.0)
        self.assertIsNone(models[1].completion_price)


class CacheControlTests(unittest.TestCase):
    def test_only_anthropic_models_get_breakpoints(self):
        messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
        self.assertEqual(apply_cache_control("openai/gpt", messages), messages)

    def test_marks_system_and_last_text_message_not_tool(self):
        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
        ]
        out = apply_cache_control("anthropic/claude", messages)
        self.assertEqual(out[0]["content"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(out[1]["content"][0]["text"], "U")
        self.assertEqual(out[3]["content"], "out")
        self.assertEqual(messages[0]["content"], "S")


if __name__ == "__main__":
    unittest.main()
